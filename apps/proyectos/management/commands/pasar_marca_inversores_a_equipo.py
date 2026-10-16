"""Copia la marca de los inversores al equipo de inversores de cada planta.

    python manage.py pasar_marca_inversores_a_equipo            # solo lista
    python manage.py pasar_marca_inversores_a_equipo --aplicar  # copia

Por qué. Desde `proyectos/0024` la marca es del equipo de tipo inversor
(`equipos.marca`), no de cada inversor: en una planta todos son de la misma. La
columna `inversores.marca` sigue en la base hasta que esto corra.

Qué hace. Por planta, toma las marcas de sus inversores (activos y retirados):
- una sola marca: la escribe en el equipo de inversores (lo crea si no existe),
  salvo que el equipo ya tenga OTRA marca escrita: eso se lista y no se toca;
- varias marcas: no copia nada y la lista, para escoger a mano en la ficha.
Se puede correr más de una vez.
"""
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from apps.proyectos.models import Equipo, Proyecto, TipoEquipo


class Command(BaseCommand):
    help = "Copia inversores.marca a equipos.marca del equipo de inversores (solo lee sin --aplicar)."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Escribir; sin esto solo lista.")

    def handle(self, *args, **opciones):
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name = 'inversores' AND column_name = 'marca'")
            (hay_columna,) = cur.fetchone()
            if not hay_columna:
                self.stdout.write("inversores.marca ya no existe: no hay de dónde copiar.")
                return
            cur.execute("""
                SELECT proyecto_id, array_agg(DISTINCT btrim(marca))
                  FROM inversores
                 WHERE marca IS NOT NULL AND btrim(marca) <> ''
                 GROUP BY proyecto_id
            """)
            marcas = dict(cur.fetchall())

        nombres = dict(Proyecto.objects.filter(id__in=marcas).values_list("id", "nombre_comercial"))
        equipos = {e.proyecto_id: e for e in Equipo.objects.filter(proyecto_id__in=marcas, tipo=TipoEquipo.INVERSOR)}

        copiar, iguales, varias, distinta = [], 0, [], []
        for pid, lista in sorted(marcas.items(), key=lambda x: nombres.get(x[0]) or ""):
            nombre = f"#{pid} {nombres.get(pid) or ''}".strip()
            if len(lista) > 1:
                varias.append(f"  {nombre}: {', '.join(sorted(lista))}")
                continue
            marca = lista[0]
            actual = (equipos[pid].marca or "").strip() if pid in equipos else ""
            if actual.lower() == marca.lower():
                iguales += 1
            elif actual:
                distinta.append(f"  {nombre}: el equipo dice «{actual}», los inversores «{marca}»")
            else:
                copiar.append((pid, marca, nombre))

        self.stdout.write(
            f"Plantas con marca en sus inversores: {len(marcas)}. A copiar: {len(copiar)}. "
            f"Ya iguales: {iguales}. Con varias marcas: {len(varias)}. Con otra marca en el equipo: {len(distinta)}."
        )
        for _, marca, nombre in copiar:
            self.stdout.write(f"  {nombre}: {marca}")
        if varias:
            self.stdout.write("\nVarias marcas (escoger a mano en la ficha):")
            self.stdout.write("\n".join(varias))
        if distinta:
            self.stdout.write("\nEl equipo ya tiene otra marca (no se toca):")
            self.stdout.write("\n".join(distinta))

        if not opciones["aplicar"]:
            self.stdout.write("\nSolo lectura: corre con --aplicar para escribir.")
            return
        with transaction.atomic():
            for pid, marca, _ in copiar:
                equipo, _ = Equipo.objects.get_or_create(proyecto_id=pid, tipo=TipoEquipo.INVERSOR)
                equipo.marca = marca
                equipo.save(update_fields=["marca"])
        self.stdout.write(self.style.SUCCESS(f"\nCopiadas {len(copiar)} marcas."))
