"""Agrega `cedula_ciudadania` a los tipos de documento comercial del cliente.

`cliente_documentos_comerciales.tipo` no es un `varchar` en la base: es un enum
nativo de PostgreSQL heredado de SQLAlchemy (`tipo_documento_cliente_enum` en los
modelos viejos, `tipodocumentoclienteenum` en los despliegues que lo crearon por
Alembic). Django lo lee como texto y solo valida con `choices`, asi que cambiar el
modelo no basta: sin el `ALTER TYPE` la insercion falla en el driver.

El nombre del tipo se resuelve desde el catalogo para no depender de cual de los
dos nombres tiene cada base, y si la columna ya es texto no hay nada que hacer.
`atomic = False` porque `ALTER TYPE ... ADD VALUE` no puede correr dentro de una
transaccion en PostgreSQL < 12.
"""

from django.db import migrations, models

NUEVO_VALOR = "cedula_ciudadania"

BUSCAR_ENUM = """
    SELECT t.typname
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_type t ON t.oid = a.atttypid
    WHERE c.relname = 'cliente_documentos_comerciales'
      AND a.attname = 'tipo'
      AND a.attnum > 0
      AND NOT a.attisdropped
      AND t.typtype = 'e'
      AND n.nspname = ANY (current_schemas(false))
"""


def agregar_valor_enum(apps, schema_editor):
    conexion = schema_editor.connection
    if conexion.vendor != "postgresql":
        return
    with conexion.cursor() as cur:
        cur.execute(BUSCAR_ENUM)
        fila = cur.fetchone()
        if not fila:
            # La columna ya es texto: `choices` en el modelo es toda la validacion.
            return
        nombre_enum = conexion.ops.quote_name(fila[0])
        # Sin parametros: psycopg3 los liga del lado del servidor y el DDL no los admite.
        cur.execute(f"ALTER TYPE {nombre_enum} ADD VALUE IF NOT EXISTS '{NUEVO_VALOR}'")


def quitar_valor_enum(apps, schema_editor):
    # PostgreSQL no sabe quitar un valor de un enum sin recrear el tipo entero.
    # Dejar el valor de mas es inofensivo: ningun documento lo usa tras el downgrade.
    pass


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ("clientes", "0003_initial"),
    ]

    operations = [
        migrations.RunPython(agregar_valor_enum, quitar_valor_enum),
        migrations.AlterField(
            model_name="clientedocumentocomercial",
            name="tipo",
            field=models.CharField(
                choices=[
                    ("rut", "rut"),
                    ("cedula_ciudadania", "cedula_ciudadania"),
                    ("certificado_bancario", "certificado_bancario"),
                    ("camara_comercio", "camara_comercio"),
                    ("oferta", "oferta"),
                    ("contrato", "contrato"),
                ],
                max_length=20,
            ),
        ),
    ]
