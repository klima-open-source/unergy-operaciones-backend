"""La factura de un contrato dice a QUE CLIENTE se le emite, no solo un nombre.

`contrato_factura.inversionista` era texto escrito a mano: sin NIT y sin forma de
saber a quien corresponde. Es la misma enfermedad de las partes de un contrato
(`docs/SERVICIOS_AGRUPACION.md` §4-decies), una capa mas abajo -- y en facturas,
donde el NIT hace falta de verdad.

Queda como en `ContratoServicio`: `inversionista` es el vinculo y
`inversionista_nombre` la copia del texto. Se eligio renombrar en vez de agregar
una segunda columna con otro nombre (decision de Sara, 2026-09-20): tener
`inversionista` de texto al lado de un vinculo llamado de otra forma deja dos
cosas que parecen distintas y son la misma.

**Escrita a mano**: `makemigrations` no puede saber que `inversionista_nombre` es
el viejo `inversionista` renombrado. Si se deja adivinar, genera un ADD COLUMN de
una columna que ya existe, y eso falla al desplegar.

El orden importa: primero se libera el nombre `inversionista` renombrando la
columna de texto, y solo despues se crea la clave foranea con ese nombre.

La columna nueva nace NULL y nadie la exige: las 343 filas existentes siguen
funcionando, con su nombre intacto en `inversionista_nombre`. Se puebla con
`manage.py vincular_partes_contratos`.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clientes", "0004_documento_cedula_ciudadania"),
        ("facturacion", "0002_alter_facturaagrupacion_codigo_sic_contrato"),
    ]

    operations = [
        migrations.RenameField(
            model_name="contratofactura",
            old_name="inversionista",
            new_name="inversionista_nombre",
        ),
        migrations.AddField(
            model_name="contratofactura",
            name="inversionista",
            field=models.ForeignKey(
                blank=True,
                db_column="inversionista_id",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="facturas_de_contrato",
                to="clientes.cliente",
            ),
        ),
    ]
