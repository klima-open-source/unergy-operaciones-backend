"""`contratos.portafolio` sale del modelo (portafolios, paso 1).

Texto escrito a mano en 56 contratos de representación ("Ayura 1", "Sol de la
Sierra 1"…). Ninguna API lo devolvía ni lo guardaba: la pantalla de
Representación lo mostraba siempre vacío. El portafolio de un contrato se deduce
de sus plantas. Solo estado: la columna sigue en la base hasta `0026`, después de
que `copiar_portafolios` imprima su respaldo.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('contratos', '0020_responsable_fuera_del_estado'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RemoveField(model_name='contrato', name='portafolio')],
        ),
    ]
