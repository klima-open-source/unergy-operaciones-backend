"""Las tarifas del contrato dejan de ser columnas para Django (servicios y tarifas, paso 2).

    tarifa_base, tarifa_representacion, tarifa_cgm, tarifa_admin, tarifa_mensual,
    indexacion_representacion, indexacion_cgm, indexacion_anual, indexacion_mensual
        -> la base en `servicios.tarifa_base`, los años en `tarifas`; se leen y se
           escriben por atajos con el mismo nombre

SOLO ESTADO, igual que `0018` y `0013`: las columnas se quedan en la base, con sus
datos, y nadie las escribe; revertir este despliegue no pierde nada. El DROP va en
el paso 3. Lo mismo hace `ppa/0005` con `ppa_tarifas`.

Antes de desplegar esto, en el servidor: `registrar_tarifas --aplicar` (paso 1). Sin
eso los contratos quedarían sin tarifas para el código.
"""
from django.db import migrations

QUITAR = (
    "tarifa_base", "indexacion_anual", "indexacion_cgm", "indexacion_mensual", "indexacion_representacion",
    "tarifa_admin", "tarifa_cgm", "tarifa_mensual", "tarifa_representacion",
)


class Migration(migrations.Migration):

    dependencies = [
        ('contratos', '0021_portafolio_fuera_del_estado'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.RemoveConstraint(model_name='contrato', name='ck_contratos_tarifa'),
                *[migrations.RemoveField(model_name='contrato', name=campo) for campo in QUITAR],
            ],
        ),
    ]
