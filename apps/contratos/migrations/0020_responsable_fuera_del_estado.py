"""El responsable del PPA deja de ser columna para Django (retiro de `ppa_responsables`, paso 1).

    contratos.responsable_id  -> nadie la lee ni la escribe

Decisión de Sara (2026-10-14): Cumplimiento deja de esconder los contratos
"externos" (los que gestiona un tercero) y no se reemplaza por ninguna bandera:
todas las vistas trabajan con todos los contratos PPA vigentes, como hacían con
`?incluir_todos=true`. El catálogo `ppa_responsables` solo existía para ese filtro.

SOLO ESTADO, igual que `0021` y `0018`: la columna se queda en la base, con sus
datos, y nadie la escribe; revertir este despliegue no pierde nada. El DROP va en
el paso 2 (`0027_borrar_responsables`). Lo mismo hace `ppa/0004` con el modelo.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('contratos', '0019_servicios_administracion_y_tarifas'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[migrations.RemoveField(model_name='contrato', name='responsable')],
        ),
    ]
