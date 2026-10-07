"""Las 12 columnas de las partes salen del modelo, pero SIGUEN en la base.

Comprador, vendedor, contratante y prestador —con su `_id`, `_nombre` y `_nit`— pasan a
`contrato_partes` y a la ficha del cliente (rama `contratos-partes`, 2026-10-07). Esta
migración solo le dice a Django que ya no existen: no toca la base. Así, si el
despliegue del código hay que revertirlo, los datos siguen ahí.

El DROP real va en una migración posterior, en su propio despliegue, cuando el código
nuevo ya esté funcionando (como `contratos-d3` con las tablas viejas).
"""
from django.db import migrations

CAMPOS = [
    f"{rol}{sufijo}"
    for rol in ("comprador", "vendedor", "contratante", "prestador")
    for sufijo in ("", "_nombre", "_nit")
]


class Migration(migrations.Migration):

    dependencies = [
        ("contratos", "0012_contrato_partes"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="contrato", name=campo) for campo in CAMPOS
            ],
            database_operations=[],
        ),
    ]
