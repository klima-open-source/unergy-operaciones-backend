"""Borra las columnas de `proyectos` que `0021_columnas_fuera_del_estado` sacó del
modelo (Sara, 2026-10-16). IRREVERSIBLE.

    srv_operacion, srv_representacion, srv_cgm, srv_ppa   -> los servicios salen de los contratos
    potencia_con_cen_mw, codigo_cnd                       -> vacías en todas las plantas
    fecha_fin_representacion                              -> la pone el contrato de representación
    fecha_comercializacion_editada_manual                 -> el cálculo solo llena fechas vacías

Nadie las lee ni las escribe desde el 15-oct. Sara tomó el respaldo de sus valores
(`respaldar_columnas_proyectos`) antes de este despliegue; el comando se va aquí.
"""
from django.db import migrations

COLUMNAS = (
    "srv_operacion", "srv_representacion", "srv_cgm", "srv_ppa",
    "potencia_con_cen_mw", "codigo_cnd",
    "fecha_fin_representacion", "fecha_comercializacion_editada_manual",
)


class Migration(migrations.Migration):

    dependencies = [
        ('proyectos', '0024_marca_de_inversores_fuera_del_estado'),
    ]

    operations = [
        # Sin reverse_sql: no hay vuelta atrás por migración, solo por el respaldo.
        migrations.RunSQL(
            "ALTER TABLE proyectos " + ", ".join(f"DROP COLUMN IF EXISTS {c}" for c in COLUMNAS) + ";"
        ),
    ]
