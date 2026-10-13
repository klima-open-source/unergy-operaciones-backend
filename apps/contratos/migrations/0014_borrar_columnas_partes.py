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
   que tienen el nombre escrito pero ningún cliente. Solo existen en estas columnas; el
   CSV es lo que queda para vincularlas después (decisión de Sara, 2026-10-13).
2. El respaldo completo de las columnas, fuera de la base:

       docker compose exec <servicio de postgres o host con acceso> psql <base> -c \\
           "\\copy (SELECT id, comprador_id, comprador_nombre, comprador_nit,
                          vendedor_id, vendedor_nombre, vendedor_nit,
                          contratante_id, contratante_nombre, contratante_nit,
                          prestador_id, prestador_nombre, prestador_nit
                     FROM contratos) TO 'contratos_partes_columnas_<fecha>.csv' CSV HEADER"

Antes de borrar comprueba que cada parte con cliente esté en `contrato_partes` con ese
mismo cliente. Si falta alguna, se detiene sin borrar nada: borrar sería perder el único
lugar donde está ese vínculo.
"""
from django.db import migrations

ROLES = ("comprador", "vendedor", "contratante", "prestador")
SUFIJOS = ("_id", "_nombre", "_nit")

COMPROBAR = "\n".join(
    f"""
    SELECT count(*) INTO faltan
      FROM contratos k
     WHERE k.{rol}_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM contrato_partes p
                        WHERE p.contrato_id = k.id AND p.rol = '{rol}'
                          AND p.cliente_id = k.{rol}_id);
    IF faltan > 0 THEN
        RAISE EXCEPTION '% contratos tienen un {rol} con cliente que no está en contrato_partes: '
            'no se borra nada.', faltan;
    END IF;"""
    for rol in ROLES
)

SOLTAR = ",\n".join(f"    DROP COLUMN {rol}{sufijo}" for rol in ROLES for sufijo in SUFIJOS)

BORRAR = f"""
DO $$
DECLARE
    faltan integer;
BEGIN
{COMPROBAR}
END
$$;
ALTER TABLE contratos
{SOLTAR};
"""


class Migration(migrations.Migration):

    dependencies = [
        ("contratos", "0013_partes_fuera_del_estado"),
    ]

    operations = [
        # Sin reverse_sql: no hay vuelta atrás por migración, solo por el respaldo.
        migrations.RunSQL(BORRAR),
    ]
