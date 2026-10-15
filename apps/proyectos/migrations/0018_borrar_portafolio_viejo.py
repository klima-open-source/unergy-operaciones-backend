"""Borra la pertenencia vieja a portafolios (portafolios, paso 2). IRREVERSIBLE.

    proyectos.portafolio_id    (con su llave a `portafolios`)
    portafolios.activo

`0013_portafolio_proyectos` las sacó del modelo; la pertenencia vive en
`portafolio_proyectos`, donde la copió `copiar_portafolios --aplicar`.

Se detiene sin borrar si alguna planta tiene en `portafolio_id` un portafolio que
no está en `portafolio_proyectos`: sería perder la asignación de la planta.
"""
from django.db import migrations

FALTAN = """
SELECT p.id, p.nombre_comercial, p.portafolio_id
  FROM proyectos p
 WHERE p.portafolio_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM portafolio_proyectos pp
                    WHERE pp.proyecto_id = p.id AND pp.portafolio_id = p.portafolio_id)
"""


def frenar_si_falta_copiar(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        cur.execute(FALTAN)
        faltan = cur.fetchall()
    if faltan:
        muestra = ", ".join(f"#{pid} {nombre}" for pid, nombre, _ in faltan[:5])
        raise RuntimeError(
            f"[proyectos/0018] {len(faltan)} plantas tienen un portafolio que no está en "
            f"portafolio_proyectos ({muestra}…): corre copiar_portafolios --aplicar antes "
            "de borrar la columna. No se borra nada."
        )


class Migration(migrations.Migration):

    dependencies = [
        ('proyectos', '0017_borrar_es_patrimonio_autonomo'),
    ]

    operations = [
        migrations.RunPython(frenar_si_falta_copiar, migrations.RunPython.noop),
        # Sin reverse_sql: no hay vuelta atrás por migración.
        migrations.RunSQL(
            "ALTER TABLE proyectos DROP COLUMN portafolio_id;"
            "ALTER TABLE portafolios DROP COLUMN activo;"
        ),
    ]
