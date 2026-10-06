"""`arr_arrendador.cliente_id`: el arrendador deja de ser solo un nombre.

El arrendador es la SEXTA parte de un contrato, y era la unica sin forma de
vincularse a `clientes`. Factura --cada uno su parte, con su propio IVA-- y de
el solo se guardaba `nombre`: ni NIT, ni razon social, ni documentos, asi que
quien emitia la factura buscaba el NIT fuera del sistema.

La columna nace NULL y nadie la exige en la base: las filas existentes siguen
funcionando igual. Se puebla con `manage.py vincular_partes_contratos`, que
cruza por nombre y deja para revision manual lo que no empareje.

Ver `docs/SERVICIOS_AGRUPACION.md` §4-decies.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('arriendos', '0003_initial'),
        ('clientes', '0004_documento_cedula_ciudadania'),
    ]

    operations = [
        migrations.AddField(
            model_name='arrarrendador',
            name='cliente',
            field=models.ForeignKey(blank=True, db_column='cliente_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='arrendamientos', to='clientes.cliente'),
        ),
    ]
