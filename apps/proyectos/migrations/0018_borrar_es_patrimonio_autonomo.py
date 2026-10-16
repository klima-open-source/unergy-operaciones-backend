"""Borra `proyecto_inversionistas.es_patrimonio_autonomo` (patrimonios, paso 2). IRREVERSIBLE.

`0012_patrimonios` la sacó del modelo; la reemplaza `patrimonio_id`, que además
dice cuál patrimonio. Estaba en false en las 116 participaciones al 2026-10-13 y
nadie la leía: no hay nada que copiar.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('proyectos', '0017_portafolio_originabot'),
    ]

    operations = [
        # Sin reverse_sql: no hay vuelta atrás por migración.
        migrations.RunSQL(
            "ALTER TABLE proyecto_inversionistas DROP COLUMN es_patrimonio_autonomo;"
        ),
    ]
