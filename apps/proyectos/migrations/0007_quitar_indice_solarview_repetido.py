"""`project_id_solarview` tenía DOS índices únicos idénticos.

Verificado contra producción el 2026-09-15:

    ix_proyectos_project_id_solarview  UNIQUE (project_id_solarview)
                                       WHERE project_id_solarview IS NOT NULL
    uq_proyectos_project_id_solarview  UNIQUE (project_id_solarview)

Hacen lo mismo. En Postgres un índice único ya permite varios `NULL` --`NULL`
nunca es igual a `NULL`--, así que el `WHERE ... IS NOT NULL` no cambia qué
rechaza: los dos dejan pasar todos los nulos y prohíben un valor repetido.

No causaba errores, solo desperdicio: cada alta o edición de un proyecto
mantenía los dos.

**Se conserva el parcial y se va el completo.** 151 de 188 proyectos tienen ese
id en `NULL`, y el parcial no los indexa: es el mismo control con un índice
mucho más chico.

Ninguno de los dos lo conoce Django -- el campo es un `CharField` sin `unique`
ni `db_index`, y ninguna migración los crea. Vienen del esquema que Django
adoptó con `--fake-initial`. Por eso esto es `RunSQL` y no una operación de
modelo: no hay estado de Django que actualizar.

Las dos formas de borrado, las dos con `IF EXISTS`: desde `pg_index` no se
distingue un índice suelto de uno que respalda una restricción de tabla, y cada
caso se quita con una sentencia distinta. Correr las dos es seguro e
idempotente.
"""
from django.db import migrations

QUITAR = """
ALTER TABLE proyectos DROP CONSTRAINT IF EXISTS uq_proyectos_project_id_solarview;
DROP INDEX IF EXISTS uq_proyectos_project_id_solarview;
"""

DEVOLVER = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_proyectos_project_id_solarview
    ON proyectos USING btree (project_id_solarview);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("proyectos", "0006_unicos_codigo_tsf_y_origina"),
    ]

    operations = [
        migrations.RunSQL(sql=QUITAR, reverse_sql=DEVOLVER),
    ]
