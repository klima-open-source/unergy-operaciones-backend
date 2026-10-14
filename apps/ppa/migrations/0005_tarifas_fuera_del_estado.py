"""Los precios mes a mes del PPA dejan `ppa_tarifas` para Django (servicios y tarifas, paso 2).

    ppa_tarifas (contrato_id, año, mes, tarifa)
        -> `tarifas` (servicio_id de compra o venta, anio, mes, valor)

SOLO ESTADO, igual que `contratos/0022`: la tabla se queda en la base, con sus
datos, y nadie la escribe; revertir este despliegue no pierde nada. El DROP va en
el paso 3 (`contratos/0022`).

Antes de desplegar esto, en el servidor: `registrar_tarifas --aplicar` (paso 1).
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('ppa', '0004_responsables_fuera_del_estado'),
        ('contratos', '0022_tarifas_fuera_del_estado'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[migrations.DeleteModel(name='PpaTarifa')],
        ),
    ]
