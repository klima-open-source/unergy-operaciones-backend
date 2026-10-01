"""Crea `interruptor_reconectadores`: el interruptor del ON/OFF, editable por admin.

Tabla nueva de una sola fila. No crea la fila: mientras no exista, el
interruptor se lee como apagado (ver `services/reconectadores.py`), así que
la migración no cambia nada de lo que hace producción hoy.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('monitoreo', '0006_eliminar_mantenimientos'),
    ]

    operations = [
        migrations.CreateModel(
            name='InterruptorReconectadores',
            fields=[
                ('id', models.SmallIntegerField(default=1, primary_key=True, serialize=False)),
                ('habilitado', models.BooleanField(default=False)),
                ('actualizado_por', models.CharField(blank=True, max_length=255, null=True)),
                ('actualizado_en', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'interruptor_reconectadores',
            },
        ),
    ]
