"""Copia `proyecto_info_tecnica` a `proyectos` y `equipos`, y enlaza los inversores.

**Por defecto solo lee.** Escribe únicamente con `--aplicar`.

    python manage.py pasar_info_tecnica_a_equipos
    python manage.py pasar_info_tecnica_a_equipos --aplicar

Para qué. `proyectos/0021` reparte la vieja info técnica (una fila por planta con
la marca de cada tipo de equipo) en `proyectos.capacidad_instalada_kwp` y
`voltaje_red`, en `equipos` (una fila por tipo) y en `inversores`, que cuelgan del
equipo de tipo inversor. Este comando copia los datos, una vez:

- solo llena lo que está vacío en el lugar nuevo: no pisa lo que alguien ya
  cargó después del despliegue, y correrlo dos veces no cambia nada;
- cada inversor queda enlazado al equipo de inversores de su planta, que se crea
  si no existe, y la cantidad de ese equipo es la de inversores activos;
- la potencia del panel que no es un número no se copia: se lista.

Ya no se copian `potencia_inversores_kwp` (la potencia es la de cada inversor),
`tiene_internet` ni `url_ubicacion`: solo se cuenta cuántas plantas los tenían.
Hay que respaldar la tabla antes del despliegue que la borra.

Lee la tabla con SQL directo: ya no es del modelo. No imprime las IP de los
módems: el repo y sus logs de Actions son públicos.
"""
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from apps.proyectos.models import Equipo, Proyecto, ProyectoInversor
from apps.proyectos.services import equipos as equipos_svc

CAMPOS = [*equipos_svc.DEL_PROYECTO, *equipos_svc.MAPA, equipos_svc.MARCA_INVERSORES]
DESCARTADOS = list(equipos_svc.DESCARTADOS)


class Command(BaseCommand):
    help = "Copia proyecto_info_tecnica a proyectos y equipos (solo lee sin --aplicar)."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Escribe los cambios.")

    def handle(self, *args, aplicar=False, **opciones):
        with connection.cursor() as cur:
            cur.execute("SELECT to_regclass('proyecto_info_tecnica')")
            if cur.fetchone()[0] is None:
                self.stdout.write("proyecto_info_tecnica ya no existe: no hay de dónde copiar.")
                return
            # tipo_tracker es un enum de Postgres: se lee como texto.
            columnas = ", ".join(f"{c}::text" if c == "tipo_tracker" else c for c in CAMPOS)
            cur.execute(f"SELECT proyecto_id, {columnas} FROM proyecto_info_tecnica")
            fichas = {fila[0]: dict(zip(CAMPOS, fila[1:])) for fila in cur.fetchall()}
            descartados = {}
            for campo in DESCARTADOS:
                vacio = "IS TRUE" if campo == "tiene_internet" else "<> ''"
                cur.execute(f"SELECT count(*) FROM proyecto_info_tecnica WHERE {campo} {vacio}")
                descartados[campo] = cur.fetchone()[0]

        con_cambios, avisos, cantidades = [], [], []
        with transaction.atomic():
            equipos_antes = Equipo.objects.count()
            proyectos = (Proyecto.objects.filter(id__in=fichas)
                         .prefetch_related("equipos", "inversores").order_by("nombre_comercial"))
            for proyecto in proyectos:
                vieja = fichas[proyecto.id]
                actual = equipos_svc.ficha_tecnica(proyecto) or {}
                datos = {c: v for c, v in vieja.items()
                         if not _vacio(v) and actual.get(c) is None}
                if not datos:
                    continue
                con_cambios.append((proyecto, sorted(datos)))
                for aviso in equipos_svc.guardar_ficha_tecnica(proyecto, datos):
                    avisos.append(f"#{proyecto.id} {proyecto.nombre_comercial}: {aviso}")

            enlazados = 0
            sin_equipo = (ProyectoInversor.objects.filter(equipo__isnull=True)
                          .values_list("proyecto_id", flat=True).distinct())
            for proyecto_id in sorted(set(sin_equipo)):
                equipo = equipos_svc.equipo_de_inversores(proyecto_id)
                enlazados += ProyectoInversor.objects.filter(
                    proyecto_id=proyecto_id, equipo__isnull=True).update(equipo=equipo)
                equipos_svc.actualizar_cantidad_inversores(proyecto_id)

            # La cantidad que decía la ficha contra los inversores cargados: manda
            # la de los inversores, pero la diferencia se lista.
            cargados = {e.proyecto_id: e.cantidad for e in Equipo.objects.filter(
                tipo=equipos_svc.T.INVERSOR).select_related("proyecto")}
            for proyecto in Proyecto.objects.filter(id__in=cargados).order_by("nombre_comercial"):
                declarada = fichas.get(proyecto.id, {}).get("cantidad_inversores")
                if declarada is not None and cargados[proyecto.id] not in (None, declarada):
                    cantidades.append(f"#{proyecto.id} {proyecto.nombre_comercial}: "
                                      f"la ficha decía {declarada}, hay {cargados[proyecto.id]} cargados")
            equipos_creados = Equipo.objects.count() - equipos_antes
            if not aplicar:
                transaction.set_rollback(True)

        self.stdout.write(
            f"Fichas en proyecto_info_tecnica: {len(fichas)}. Plantas con algo que copiar: "
            f"{len(con_cambios)}. Equipos nuevos: {equipos_creados}. "
            f"Inversores enlazados a su equipo: {enlazados}."
        )
        for proyecto, campos in con_cambios:
            self.stdout.write(f"  #{proyecto.id} {proyecto.nombre_comercial}: {', '.join(campos)}")
        if avisos:
            self.stdout.write("\nNo se copiaron (corregir a mano):")
            for aviso in avisos:
                self.stdout.write(f"  {aviso}")
        if cantidades:
            self.stdout.write("\nCantidad de inversores distinta (queda la de los cargados):")
            for linea in cantidades:
                self.stdout.write(f"  {linea}")
        self.stdout.write("\nYa no se guardan (plantas que tenían valor): " + ", ".join(
            f"{campo} {n}" for campo, n in descartados.items()))
        if not aplicar:
            self.stdout.write("\nSolo lectura: corre con --aplicar para escribir.")


def _vacio(valor) -> bool:
    return valor is None or (isinstance(valor, str) and not valor.strip())
