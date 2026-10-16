"""Borra `portafolios.activo`. IRREVERSIBLE.

`0013_portafolio_proyectos` la sacó del modelo: estaba en true en los 24
portafolios y solo filtraba el asistente de informes.

`proyectos.portafolio_id` NO se borra (decisión de Sara, 2026-10-15): un proyecto
vuelve a estar en un solo portafolio, y esa columna vuelve a ser la relación en
la ronda de reestructura. Hasta entonces sigue en la base, sin escribirse.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('proyectos', '0018_borrar_es_patrimonio_autonomo'),
    ]

    operations = [
        # Sin reverse_sql: no hay vuelta atrás por migración.
        migrations.RunSQL("ALTER TABLE portafolios DROP COLUMN activo;"),
    ]
