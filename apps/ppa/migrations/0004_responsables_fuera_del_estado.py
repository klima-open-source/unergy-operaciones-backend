"""El catálogo de responsables de PPA deja de existir para Django (retiro de `ppa_responsables`, paso 1).

    ppa_responsables (nombre, incluir_en_cumplimiento)  -> nadie la lee ni la escribe

SOLO ESTADO, igual que `contratos/0020`: la tabla se queda en la base, con sus
datos, y nadie la escribe; revertir este despliegue no pierde nada. El DROP va en
el paso 2 (`contratos/0027_borrar_responsables`).
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('ppa', '0003_corte_estado'),
        ('contratos', '0020_responsable_fuera_del_estado'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[migrations.DeleteModel(name='PpaResponsable')],
        ),
    ]
