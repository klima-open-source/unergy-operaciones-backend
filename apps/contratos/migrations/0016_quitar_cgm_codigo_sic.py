"""Deploy 3: fuera `contratos.cgm_codigo_sic` (decisión del 2026-10-06).

Nadie lo leía —ni el detalle, ni los reportes de CGM, ni liquidaciones— y el front
dejó de pedirlo. El único contrato que lo tenía (el 1) guardaba ahí su mismo
`numero_contrato`, así que no se pierde nada. Su copia vieja queda en el dump de
`contratos_servicio` que precede a este deploy.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('contratos', '0015_borrar_tablas_viejas'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='contrato',
            name='cgm_codigo_sic',
        ),
    ]
