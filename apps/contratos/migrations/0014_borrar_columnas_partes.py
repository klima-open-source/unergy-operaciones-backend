"""Borra las 12 columnas de las partes de `contratos`. IRREVERSIBLE.

`0013_partes_fuera_del_estado` las sacó del modelo sin tocar la base, para poder revertir
el despliegue del 2026-10-07 sin perder datos. Desde entonces nada las lee ni las escribe:
las partes viven en `contrato_partes` (siempre con cliente) y el nombre y el NIT se leen
de la ficha del cliente. Esta migración hace el borrado que quedó pendiente:

    comprador_id   comprador_nombre   comprador_nit
    vendedor_id    vendedor_nombre    vendedor_nit
    contratante_id contratante_nombre contratante_nit
    prestador_id   prestador_nombre   prestador_nit

Con las `_id` se van sus llaves foráneas a `clientes` y sus índices.

Va en el despliegue de limpieza junto con `0015_borrar_tablas_viejas` (al menos dos
semanas después del corte del 2026-10-06). ANTES de desplegar, en producción:

1. `manage.py exportar_partes_sin_cliente` (está en `comando.yml`, solo lee): las partes
   que tienen el nombre escrito pero ningún cliente. Solo existen en estas columnas
   (decisión de Sara, 2026-10-13: exportar y borrar; ella guardó la lista de la copia
   local ese día).
2. El respaldo completo de las columnas, fuera de la base:

       docker compose exec <servicio de postgres o host con acceso> psql <base> -c \\
           "\\copy (SELECT id, comprador_id, comprador_nombre, comprador_nit,
                          vendedor_id, vendedor_nombre, vendedor_nit,
                          contratante_id, contratante_nombre, contratante_nit,
                          prestador_id, prestador_nombre, prestador_nit
                     FROM contratos) TO 'contratos_partes_columnas_<fecha>.csv' CSV HEADER"

**El freno.** Se detiene sin borrar nada en el único caso que de verdad perdería datos:
que `contrato_partes` esté vacía mientras las columnas sí tienen partes (nunca se corrió
`registrar_partes_contratos --aplicar`). Las diferencias contrato por contrato NO lo
detienen: desde el 2026-10-08 las pantallas escriben solo en `contrato_partes` y estas
columnas quedaron congeladas, así que un contratante cambiado después aparece como
diferencia sin que se pierda nada. Se imprimen en el log del despliegue como aviso.
(Corrección del 2026-10-13: antes cualquier diferencia detenía el despliegue.)
"""
from django.db import migrations

ROLES = ("comprador", "vendedor", "contratante", "prestador")
SUFIJOS = ("_id", "_nombre", "_nit")


def comprobar(apps, schema_editor):
    """Avisa de las diferencias; solo se detiene si `contrato_partes` está vacía."""
    with schema_editor.connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM contrato_partes")
        en_la_tabla = cur.fetchone()[0]
        en_columnas = 0
        for rol in ROLES:
            cur.execute(f"SELECT count(*) FROM contratos WHERE {rol}_id IS NOT NULL")
            en_columnas += cur.fetchone()[0]
            cur.execute(
                f"""SELECT count(*) FROM contratos k
                     WHERE k.{rol}_id IS NOT NULL
                       AND NOT EXISTS (SELECT 1 FROM contrato_partes p
                                        WHERE p.contrato_id = k.id AND p.rol = %s
                                          AND p.cliente_id = k.{rol}_id)""",
                [rol],
            )
            distintas = cur.fetchone()[0]
            if distintas:
                print(f"\n  [contratos/0014] Aviso: {distintas} contratos con un {rol} en la "
                      "columna vieja que no coincide con contrato_partes (editados después "
                      "del 2026-10-08). No se pierde nada: manda contrato_partes.")
    if en_columnas and not en_la_tabla:
        raise RuntimeError(
            f"contrato_partes está vacía y las columnas tienen {en_columnas} partes: corre "
            "registrar_partes_contratos --aplicar antes de borrarlas. No se borra nada."
        )


SOLTAR = ",\n".join(f"    DROP COLUMN {rol}{sufijo}" for rol in ROLES for sufijo in SUFIJOS)

BORRAR = f"""
ALTER TABLE contratos
{SOLTAR};
"""


class Migration(migrations.Migration):

    dependencies = [
        ("contratos", "0013_partes_fuera_del_estado"),
    ]

    operations = [
        migrations.RunPython(comprobar, migrations.RunPython.noop),
        # Sin reverse_sql: no hay vuelta atrás por migración, solo por el respaldo.
        migrations.RunSQL(BORRAR),
    ]
