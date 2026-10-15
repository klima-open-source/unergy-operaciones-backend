"""Borra `contratos.responsable_id` y la tabla `ppa_responsables` (retiro de responsables, paso 2). IRREVERSIBLE.

`contratos/0020` y `ppa/0004` los sacaron del modelo sin tocar la base; desde
entonces nadie los lee ni los escribe: Cumplimiento ya no esconde contratos por
empresa responsable (decisión del equipo, 2026-10-14).

No hay nada que copiar a otro lado, así que no lleva freno. Lo que se pierde es la
clasificación Unergy/Externo de cada PPA (26/7 al 2026-10-13). Respaldo opcional
antes de desplegar:

    \\copy (SELECT id, responsable_id FROM contratos WHERE responsable_id IS NOT NULL)
        TO 'contratos_responsable_<fecha>.csv' CSV HEADER
    \\copy ppa_responsables TO 'ppa_responsables_<fecha>.csv' CSV HEADER
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('contratos', '0026_borrar_columnas_de_tarifas'),
        ('ppa', '0005_tarifas_fuera_del_estado'),
    ]

    operations = [
        # Sin reverse_sql: no hay vuelta atrás por migración, solo por el respaldo.
        migrations.RunSQL(
            "ALTER TABLE contratos DROP COLUMN responsable_id;\n"
            "DROP TABLE ppa_responsables;\n"
        ),
    ]
