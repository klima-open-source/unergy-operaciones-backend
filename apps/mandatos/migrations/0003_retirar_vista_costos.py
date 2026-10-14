"""Borra las tablas de la vista vieja de Costos › Mandatos. IRREVERSIBLE.

    mandatos                 31 filas: una carga de prueba de un ZIP (24-jun-2026,
                             período 2025-05), ninguna editada después
    mandato_inversionistas   21 nombres cortos con los que esa carga adivinaba el
                             inversionista por el nombre del PDF

La vista se usó esa sola vez y la reemplazó Liquidaciones › Mandatos, que lee el
correo y escribe en `finanzas_mandatos` (confirmado por Sara, 2026-10-13). Esa
tabla, y la bitácora del correo (`mandato_correos`), NO se tocan.

**La guarda no es adorno.** Si `mandatos` tiene algo distinto de esa carga de
prueba --una fila creada o editada después del 2026-06-25--, alguien sí la usó:
la migración FALLA y el deploy se detiene, en vez de borrar un dato que nadie
podría recuperar. Mismo patrón que `proyectos/0008`.

Respaldo opcional antes de desplegar (los 31 registros de prueba):

    \\copy mandatos TO 'mandatos_vista_costos.csv' CSV HEADER
    \\copy mandato_inversionistas TO 'mandato_inversionistas.csv' CSV HEADER

Los PDF que esa vista guardaba en `uploads/mandatos/` del servidor quedan en disco;
se pueden borrar a mano.
"""
from django.db import migrations

COMPROBAR = """
DO $$
DECLARE
    usadas integer;
BEGIN
    SELECT count(*) INTO usadas FROM mandatos
     WHERE created_at >= '2026-06-25' OR updated_at >= '2026-06-25';
    IF usadas > 0 THEN
        RAISE EXCEPTION '% mandatos de la vista de Costos se crearon o editaron después de '
            'la carga de prueba: alguien la usa. No se borra nada.', usadas;
    END IF;
END
$$;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('mandatos', '0002_alter_mandatocorreo_detalle'),
    ]

    operations = [
        # Sin vuelta atrás: no hay reverse_sql para la guarda ni datos que recrear.
        migrations.RunSQL(COMPROBAR),
        migrations.DeleteModel(
            name='Mandato',
        ),
        migrations.DeleteModel(
            name='MandatoInversionista',
        ),
    ]
