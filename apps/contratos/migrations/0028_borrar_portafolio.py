"""Borra `contratos.portafolio` (portafolios, paso 2). IRREVERSIBLE.

Texto escrito a mano en 56 contratos de representación, que ninguna API leía ni
guardaba; `0021_portafolio_fuera_del_estado` lo sacó del modelo. El portafolio de
un contrato se deduce de sus plantas (`portafolio_proyectos`).

ANTES de desplegar: el respaldo en CSV lo imprime `copiar_portafolios` (en
`comando.yml`, solo lee); guardarlo del log del run.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('contratos', '0027_borrar_responsables'),
    ]

    operations = [
        # Sin reverse_sql: no hay vuelta atrás por migración.
        migrations.RunSQL("ALTER TABLE contratos DROP COLUMN portafolio;"),
    ]
