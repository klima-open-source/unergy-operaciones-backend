"""Columnas de `proyectos` que salen del modelo (Sara, 2026-10-15).

    srv_operacion, srv_representacion, srv_cgm, srv_ppa   -> los servicios salen de los contratos
    potencia_con_cen_mw, codigo_cnd                       -> vacías en todas las plantas
    fecha_fin_representacion                              -> la pone el contrato de representación
    fecha_comercializacion_editada_manual                 -> el cálculo solo llena fechas vacías

Solo estado: las columnas siguen en la base, sin leerse ni escribirse, hasta el
despliegue de los borrados (con `portafolio_proyectos`). Antes de ese despliegue,
`respaldar_columnas_proyectos` imprime sus valores en CSV.

Las `srv_*` son NOT NULL sin DEFAULT: sin este DEFAULT, crear un proyecto fallaría
porque Django ya no las manda. `fecha_comercializacion_editada_manual` ya tiene
DEFAULT false en la base.
"""
from django.db import migrations

SRV = ("srv_operacion", "srv_representacion", "srv_cgm", "srv_ppa")


class Migration(migrations.Migration):

    dependencies = [
        ('proyectos', '0019_portafolio_unico'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunSQL(
                "".join(f"ALTER TABLE proyectos ALTER COLUMN {c} SET DEFAULT false;" for c in SRV),
                migrations.RunSQL.noop,
            )],
            state_operations=[
                migrations.RemoveField(model_name='proyecto', name=campo)
                for campo in (*SRV, 'potencia_con_cen_mw', 'codigo_cnd',
                              'fecha_fin_representacion', 'fecha_comercializacion_editada_manual')
            ],
        ),
    ]
