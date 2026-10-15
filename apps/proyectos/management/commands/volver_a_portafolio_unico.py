"""Copia las asignaciones de `portafolio_proyectos` a `proyectos.portafolio_id`.

**Por defecto solo lee.** Escribe únicamente con `--aplicar`.

    python manage.py volver_a_portafolio_unico
    python manage.py volver_a_portafolio_unico --aplicar

Para qué. Del 15-oct (subida 1) a la ronda de reestructura una planta pudo estar
en varios portafolios, y lo que se asignó en ese tiempo quedó solo en
`portafolio_proyectos`. Sara decidió volver a un portafolio por proyecto
(`proyectos/0019`); este comando deja la columna igual a la tabla:

- la planta está en UN portafolio de la tabla -> la columna queda con ese;
- no está en ninguno -> la columna queda vacía (la sacaron de su portafolio);
- está en VARIOS -> no se toca: se lista para que alguien elija cuál se queda
  (en Proyectos › Portafolios o en la ficha del proyecto).

Lee la tabla con SQL directo: ya no es del modelo. Idempotente. Después de que se
borre la tabla dice que no hay de dónde copiar.
"""
from django.db import connection, transaction
from django.core.management.base import BaseCommand

PERTENENCIAS = """
SELECT p.id, p.nombre_comercial, p.portafolio_id,
       coalesce(array_agg(pp.portafolio_id ORDER BY pp.portafolio_id)
                FILTER (WHERE pp.portafolio_id IS NOT NULL), '{}')
  FROM proyectos p
  LEFT JOIN portafolio_proyectos pp ON pp.proyecto_id = p.id
 GROUP BY p.id, p.nombre_comercial, p.portafolio_id
 ORDER BY p.nombre_comercial
"""


class Command(BaseCommand):
    help = "Copia portafolio_proyectos a proyectos.portafolio_id (solo lee sin --aplicar)."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Escribe los cambios.")

    def handle(self, *args, aplicar=False, **opciones):
        with connection.cursor() as cur:
            cur.execute("SELECT to_regclass('portafolio_proyectos')")
            if cur.fetchone()[0] is None:
                self.stdout.write("portafolio_proyectos ya no existe: no hay de dónde copiar.")
                return

        cambios, varios = [], []
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(PERTENENCIAS)
            nombres = {}
            for pid, nombre, actual, en_tabla in cur.fetchall():
                nombres[pid] = nombre
                if len(en_tabla) > 1:
                    varios.append((pid, nombre, en_tabla))
                    continue
                nuevo = en_tabla[0] if en_tabla else None
                if nuevo != actual:
                    cambios.append((pid, nombre, actual, nuevo))
                    cur.execute("UPDATE proyectos SET portafolio_id = %s WHERE id = %s", [nuevo, pid])
            cur.execute("SELECT id, nombre FROM portafolios")
            portafolio = dict(cur.fetchall())
            if not aplicar:
                transaction.set_rollback(True)

        def nombre_de(pid):
            return portafolio.get(pid, "—") if pid else "ninguno"

        self.stdout.write(f"Plantas que cambian: {len(cambios)}. En varios portafolios: {len(varios)}.")
        for pid, nombre, actual, nuevo in cambios:
            self.stdout.write(f"  #{pid} {nombre}: {nombre_de(actual)} → {nombre_de(nuevo)}")
        if varios:
            self.stdout.write("\nEn varios portafolios (no se tocan; elegir uno a mano):")
            for pid, nombre, en_tabla in varios:
                self.stdout.write(f"  #{pid} {nombre}: {', '.join(nombre_de(x) for x in en_tabla)}")
        if not aplicar:
            self.stdout.write("\nSolo lectura: corre con --aplicar para escribir.")
