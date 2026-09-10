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
        migrations.RemoveField(
            model_name='promotorseguimiento',
            name='requisito',
        ),
        migrations.AlterUniqueTogether(
            name='promotorseguimiento',
            unique_together=None,
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
