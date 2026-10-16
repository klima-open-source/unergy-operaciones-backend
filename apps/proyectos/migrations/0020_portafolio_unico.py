"""Un proyecto vuelve a estar en UN portafolio (Sara, 2026-10-15).

    proyectos.portafolio_id   -> vuelve al modelo (la columna nunca se borró)
    portafolio_proyectos      -> sale del modelo; sigue en la base

Solo estado: no toca la base. La columna y su llave a `portafolios` siguen ahí
desde siempre (`0013` solo la había sacado del modelo y la subida 3 ya no la
borra). Lo que se asignó mientras hubo varios por planta está en
`portafolio_proyectos`: `volver_a_portafolio_unico --aplicar` lo copia a la
columna, y la tabla se borra en un despliegue posterior.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('proyectos', '0019_borrar_portafolios_activo'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveConstraint(
                    model_name='portafolioproyecto',
                    name='uq_portafolio_proyecto',
                ),
                migrations.RemoveField(
                    model_name='portafolio',
                    name='proyectos',
                ),
                migrations.AddField(
                    model_name='proyecto',
                    name='portafolio',
                    field=models.ForeignKey(blank=True, db_column='portafolio_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='proyectos', to='proyectos.portafolio'),
                ),
                migrations.DeleteModel(
                    name='PortafolioProyecto',
                ),
            ],
        ),
    ]
