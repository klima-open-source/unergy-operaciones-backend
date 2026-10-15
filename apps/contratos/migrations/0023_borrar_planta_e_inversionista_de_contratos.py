"""Borra la planta y el inversionista de `contratos`. IRREVERSIBLE.

    contratos.proyecto_id            (con su llave a `proyectos` y su índice)
    contratos.inversionista_id       (con su llave a `clientes` y su índice)
    contratos.inversionista_nombre

`0018_planta_e_inversionista_fuera_del_estado` (paso 4b) las sacó del modelo sin
tocar la base; desde entonces nadie las lee ni las escribe: la planta y el
inversionista viven en `contrato_proyectos` y en la participación. Esto es el paso
4c de la rama de inversionistas (2026-10-13).

ANTES de desplegar, en producción:

1. `exportar_inversionistas_de_contratos` (en `comando.yml`, solo lee): los nombres
   de inversionista escritos a mano frente a lo que dice la participación. Es lo
   único que solo vive en estas columnas.
2. Respaldo opcional de las tres columnas:

       \\copy (SELECT id, proyecto_id, inversionista_id, inversionista_nombre
                 FROM contratos WHERE grupo <> 'ppa')
           TO 'contratos_planta_inversionista_<fecha>.csv' CSV HEADER

**El freno.** Se detiene sin borrar nada en el único caso que perdería datos: que
`contrato_proyectos` no tenga ninguna fila de contratos de servicio mientras la columna
`proyecto_id` sí tiene plantas (nunca se corrió `registrar_proyectos_contratos
--aplicar`). Las diferencias contrato por contrato NO lo detienen: desde el paso 4b las
pantallas escriben solo en `contrato_proyectos` y estas columnas quedaron congeladas, así
que una planta o un inversionista cambiados después salen como diferencia sin que se
pierda nada. Se imprimen en el log del despliegue como aviso.
"""
from django.db import migrations

DIFERENCIAS = {
    "planta": """
        SELECT count(*) FROM contratos k
         WHERE k.grupo <> 'ppa' AND k.proyecto_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM contrato_proyectos cp
                            WHERE cp.contrato_id = k.id AND cp.proyecto_id = k.proyecto_id)""",
    "inversionista": """
        SELECT count(*) FROM contratos k
         WHERE k.grupo <> 'ppa' AND k.inversionista_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM contrato_proyectos cp
                 JOIN proyecto_inversionistas pi ON pi.id = cp.proyecto_inversionista_id
                 JOIN inversionistas i ON i.id = pi.inversionista_id
                WHERE cp.contrato_id = k.id AND i.cliente_id = k.inversionista_id)""",
}


def comprobar(apps, schema_editor):
    """Avisa de las diferencias; solo se detiene si nunca se llenó contrato_proyectos."""
    with schema_editor.connection.cursor() as cur:
        for que, consulta in DIFERENCIAS.items():
            cur.execute(consulta)
            distintas = cur.fetchone()[0]
            if distintas:
                print(f"\n  [contratos/0023] Aviso: {distintas} contratos con un(a) {que} en la "
                      "columna vieja que no coincide con contrato_proyectos (editados después "
                      "del paso 4b). No se pierde nada: manda contrato_proyectos.")
        cur.execute("SELECT count(*) FROM contratos WHERE grupo <> 'ppa' AND proyecto_id IS NOT NULL")
        en_columna = cur.fetchone()[0]
        cur.execute("""SELECT count(*) FROM contrato_proyectos cp
                         JOIN contratos k ON k.id = cp.contrato_id WHERE k.grupo <> 'ppa'""")
        en_la_tabla = cur.fetchone()[0]
    if en_columna and not en_la_tabla:
        raise RuntimeError(
            f"contrato_proyectos no tiene contratos de servicio y la columna tiene {en_columna} "
            "plantas: corre registrar_proyectos_contratos --aplicar (paso 4a) antes de "
            "borrarla. No se borra nada."
        )


BORRAR = """
ALTER TABLE contratos
    DROP COLUMN proyecto_id,
    DROP COLUMN inversionista_id,
    DROP COLUMN inversionista_nombre;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('contratos', '0022_tarifas_fuera_del_estado'),
    ]

    operations = [
        migrations.RunPython(comprobar, migrations.RunPython.noop),
        # Sin reverse_sql: no hay vuelta atrás por migración, solo por el respaldo.
        migrations.RunSQL(BORRAR),
    ]
