"""Elimina el servicio "promotor": la bandera y sus dos tablas.

Nunca se usó. El catálogo tenía 11 requisitos cargados y `promotor_seguimientos`
CERO filas (revisado en producción el 2026-09-10), y en el árbol Django no había
un solo endpoint que sirviera ninguna de las dos tablas -- solo aparecían en el
guard de borrado de proyectos y en la lista del merge, o sea protegiendo filas
que no existen. La bandera `srv_promotor` se leía en un lugar (el read-model del
pipeline comercial) y se mostraba como badge "PROM" en tres vistas del front.

Mismo tratamiento que `srv_rec` en la migración 0003.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('proyectos', '0003_remove_proyecto_srv_rec'),
    ]

    operations = [
        # El `AlterUniqueTogether` va PRIMERO, y no es cosmético: para borrar el
        # índice único de `(proyecto, requisito)` Django busca la columna de cada
        # campo, así que si los campos ya no están revienta con
        # `FieldDoesNotExist: PromotorSeguimiento has no field named 'requisito'`.
        # `makemigrations` las generó al revés y el deploy se cayó en el paso
        # `migrate` (run #66, 2026-09-10).
        migrations.AlterUniqueTogether(
            name='promotorseguimiento',
            unique_together=None,
        ),
        migrations.RemoveField(
            model_name='promotorseguimiento',
            name='requisito',
        ),
        migrations.RemoveField(
            model_name='promotorseguimiento',
            name='proyecto',
        ),
        migrations.RemoveField(
            model_name='proyecto',
            name='srv_promotor',
        ),
        migrations.DeleteModel(
            name='PromotorCatalogoRequisito',
        ),
        migrations.DeleteModel(
            name='PromotorSeguimiento',
        ),
    ]
