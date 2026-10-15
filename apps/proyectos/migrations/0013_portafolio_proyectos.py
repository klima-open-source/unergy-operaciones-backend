"""Portafolios (paso 1): una planta puede estar en varios portafolios.

    portafolio_proyectos (id, portafolio_id, proyecto_id)   -> nueva, vacía
    proyectos.portafolio_id                                  -> fuera del modelo
    portafolios.activo                                       -> fuera del modelo

Modelo de Sara (2026-10-15). La pertenencia era una sola columna en `proyectos`;
pasa a una fila por pareja planta–portafolio. `activo` estaba en true en los 24
portafolios y solo filtraba el asistente de informes: se quita.

Solo agrega. Las asignaciones actuales las copia `copiar_portafolios` (comando
aparte). Las dos columnas viejas siguen en la base, sin leerse, hasta `0015`.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('proyectos', '0012_patrimonios'),
    ]

    operations = [
        # `activo` es NOT NULL sin DEFAULT: sin este DEFAULT, crear un portafolio
        # fallaría porque Django ya no la manda. `portafolio_id` admite null.
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunSQL(
                "ALTER TABLE portafolios ALTER COLUMN activo SET DEFAULT true",
                migrations.RunSQL.noop,
            )],
            state_operations=[
                migrations.RemoveField(model_name='portafolio', name='activo'),
                migrations.RemoveField(model_name='proyecto', name='portafolio'),
            ],
        ),
        migrations.CreateModel(
            name='PortafolioProyecto',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('portafolio', models.ForeignKey(db_column='portafolio_id', on_delete=django.db.models.deletion.CASCADE, related_name='pertenencias', to='proyectos.portafolio')),
                ('proyecto', models.ForeignKey(db_column='proyecto_id', on_delete=django.db.models.deletion.CASCADE, related_name='pertenencias_portafolio', to='proyectos.proyecto')),
            ],
            options={
                'db_table': 'portafolio_proyectos',
            },
        ),
        migrations.AddField(
            model_name='portafolio',
            name='proyectos',
            field=models.ManyToManyField(related_name='portafolios', through='proyectos.PortafolioProyecto', to='proyectos.proyecto'),
        ),
        migrations.AddConstraint(
            model_name='portafolioproyecto',
            constraint=models.UniqueConstraint(fields=('portafolio', 'proyecto'), name='uq_portafolio_proyecto'),
        ),
    ]
