"""La planta y el inversionista del contrato dejan de ser columnas para Django.

    contratos.proyecto_id            -> contrato_proyectos.proyecto_id
    contratos.inversionista_id       -> contrato_proyectos.proyecto_inversionista_id
    contratos.inversionista_nombre   -> la ficha del cliente (por la participación)

SOLO ESTADO, igual que `0013_partes_fuera_del_estado`: las columnas se quedan en la
base, con sus datos, y nadie las escribe. Así revertir este despliegue no pierde
nada. El DROP va en `0019` (paso 4c), después de exportar los nombres de
inversionista escritos a mano que no corresponden a una participación.

Antes de desplegar esto, en el servidor: `registrar_proyectos_contratos --aplicar`
(paso 4a). Sin eso los contratos de servicio quedarían sin planta para el código.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('contratos', '0017_contrato_proyectos_con_participacion'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.RemoveField(model_name='contrato', name='inversionista'),
                migrations.RemoveField(model_name='contrato', name='inversionista_nombre'),
                migrations.RemoveField(model_name='contrato', name='proyecto'),
            ],
        ),
    ]
