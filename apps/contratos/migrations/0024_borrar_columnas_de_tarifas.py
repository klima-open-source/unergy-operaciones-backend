"""Borra las columnas de tarifas de `contratos` y la tabla `ppa_tarifas` (servicios y tarifas, paso 3). IRREVERSIBLE.

    contratos.tarifa_base, tarifa_representacion, tarifa_cgm, tarifa_admin,
    tarifa_mensual, indexacion_representacion, indexacion_cgm, indexacion_anual,
    indexacion_mensual
    ppa_tarifas

`contratos/0022` y `ppa/0005` las sacaron del modelo sin tocar la base; desde
entonces nadie las lee ni las escribe: la base y el índice viven en `servicios`, y
el valor de cada periodo (año, o año y mes en los PPA) en `tarifas`.

Lo único que se pierde sin copia son los historiales `indexacion_anual` /
`indexacion_mensual` de O&M y arriendo: una importación de Excel que ningún cálculo
usaba (los paneles indexan desde la base con las tablas de IPC) y que no cuadraba.
Respaldo opcional antes de desplegar:

    \\copy (SELECT id, grupo, servicio_aplica, tarifa_base, tarifa_mensual, tarifa_admin,
                   tarifa_cgm, tarifa_representacion, indexacion_anual, indexacion_mensual,
                   indexacion_cgm, indexacion_representacion FROM contratos)
        TO 'contratos_tarifas_columnas_<fecha>.csv' CSV HEADER
    \\copy ppa_tarifas TO 'ppa_tarifas_<fecha>.csv' CSV HEADER

**El freno.** Se detiene sin borrar nada en los dos casos que perderían datos: que
ningún servicio tenga base mientras las columnas tienen tarifas, o que `tarifas` no
tenga ningún precio por mes mientras `ppa_tarifas` sí (en los dos, nunca se corrió
`registrar_tarifas --aplicar`, paso 1). Las diferencias contrato por contrato no lo
detienen --desde el paso 2 las columnas están congeladas y una tarifa editada
después aparece como diferencia sin que se pierda nada--: salen como aviso en el log.
"""
from django.db import migrations

COLUMNAS = (
    "tarifa_base", "tarifa_representacion", "tarifa_cgm", "tarifa_admin", "tarifa_mensual",
    "indexacion_representacion", "indexacion_cgm", "indexacion_anual", "indexacion_mensual",
)

#: Columna → (servicio(s) donde quedó su valor, condición extra sobre el contrato).
BASES = {
    "tarifa_representacion": ("('representacion')", ""),
    "tarifa_cgm": ("('cgm')", ""),
    "tarifa_admin": ("('administracion')", "AND k.grupo = 'representacion_cgm'"),
    "tarifa_base": ("('compra', 'venta', 'mantenimiento', 'arriendo')",
                    "AND (k.grupo = 'ppa' OR k.servicio_aplica IN ('mantenimiento', 'arriendo'))"),
}


def comprobar(apps, schema_editor):
    """Avisa de las diferencias; solo se detiene si el paso 1 nunca se corrió."""
    with schema_editor.connection.cursor() as cur:
        for columna, (servicios, condicion) in BASES.items():
            cur.execute(
                f"""SELECT count(*) FROM contratos k
                     WHERE k.{columna} IS NOT NULL {condicion}
                       AND NOT EXISTS (SELECT 1 FROM servicios s
                                        WHERE s.contrato_id = k.id AND s.servicio IN {servicios}
                                          AND s.tarifa_base = k.{columna})"""
            )
            distintas = cur.fetchone()[0]
            if distintas:
                print(f"\n  [contratos/0024] Aviso: {distintas} contratos con {columna} en la "
                      "columna vieja distinta de la base de su servicio (editados después del "
                      "paso 2). No se pierde nada: manda el servicio.")
        cur.execute(
            """SELECT count(*) FROM ppa_tarifas p
                WHERE p.tarifa IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM servicios s JOIN tarifas t ON t.servicio_id = s.id
                                   WHERE s.contrato_id = p.contrato_id
                                     AND s.servicio IN ('compra', 'venta')
                                     AND t.anio = p."año" AND t.mes = p.mes)"""
        )
        sin_copia = cur.fetchone()[0]
        if sin_copia:
            print(f"\n  [contratos/0024] Aviso: {sin_copia} precios de ppa_tarifas sin su mes en "
                  "tarifas (borrados o editados después del paso 2).")

        cur.execute(
            "SELECT count(*) FROM contratos WHERE tarifa_base IS NOT NULL "
            "OR tarifa_representacion IS NOT NULL OR tarifa_cgm IS NOT NULL "
            "OR tarifa_admin IS NOT NULL OR tarifa_mensual IS NOT NULL"
        )
        con_tarifa = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM servicios WHERE tarifa_base IS NOT NULL")
        con_base = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM ppa_tarifas WHERE tarifa IS NOT NULL")
        en_ppa = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM tarifas WHERE mes IS NOT NULL")
        por_mes = cur.fetchone()[0]
    if con_tarifa and not con_base:
        raise RuntimeError(
            f"Ningún servicio tiene base y {con_tarifa} contratos tienen tarifas en las "
            "columnas: corre registrar_tarifas --aplicar (paso 1) antes de borrarlas. "
            "No se borra nada."
        )
    if en_ppa and not por_mes:
        raise RuntimeError(
            f"tarifas no tiene precios por mes y ppa_tarifas tiene {en_ppa}: corre "
            "registrar_tarifas --aplicar (paso 1) antes de borrarla. No se borra nada."
        )


BORRAR = (
    "ALTER TABLE contratos\n"
    + ",\n".join(f"    DROP COLUMN {c}" for c in COLUMNAS) + ";\n"
    + "DROP TABLE ppa_tarifas;\n"
)


class Migration(migrations.Migration):

    dependencies = [
        ('contratos', '0023_borrar_planta_e_inversionista_de_contratos'),
        ('ppa', '0005_tarifas_fuera_del_estado'),
    ]

    operations = [
        migrations.RunPython(comprobar, migrations.RunPython.noop),
        # Sin reverse_sql: no hay vuelta atrás por migración, solo por el respaldo.
        migrations.RunSQL(BORRAR),
    ]
