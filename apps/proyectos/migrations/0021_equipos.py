"""Equipos de la planta: reemplazan a `proyecto_info_tecnica` (Sara, 2026-10-15).

    proyectos              + capacidad_instalada_kwp, voltaje_red (venían de la info técnica)
    equipos                nueva: una fila por tipo de equipo (cantidad, marca, potencia, detalle)
    proyecto_inversores -> inversores (mismos ids: `falla_inversores` apunta a ellos)
                           + equipo_id, cantidad_strings, marca, estado
                           - activo (lo reemplaza `estado`; los 675 estaban activos)
    proyecto_info_tecnica  sale del modelo; la tabla queda

Los datos los copia `pasar_info_tecnica_a_equipos --aplicar`, a mano y una vez:
una migración de datos que falle deja el deploy a medias. `proyecto_info_tecnica`
se borra en el despliegue siguiente, con su respaldo.

`estado` lleva además DEFAULT 'activo' en la base: Django lo quita después de
llenar las filas existentes, y así un INSERT que no lo mande no falla.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('proyectos', '0020_columnas_fuera_del_estado'),
    ]

    operations = [
        migrations.AddField(
            model_name='proyecto',
            name='capacidad_instalada_kwp',
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='proyecto',
            name='voltaje_red',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.CreateModel(
            name='Equipo',
            fields=[
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Fecha de creación')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Fecha de actualización')),
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('tipo', models.CharField(choices=[('panel', 'Paneles'), ('inversor', 'Inversores'), ('transformador', 'Transformador'), ('reconectador', 'Reconectador / relé'), ('totalizador', 'Totalizador'), ('seguidor', 'Seguidor solar'), ('medidor_frontera', 'Medidores de frontera'), ('modem_reconectador', 'Módem del reconectador'), ('modem_frontera', 'Módems de frontera'), ('cctv', 'CCTV'), ('seguridad', 'Seguridad física')], max_length=30)),
                ('cantidad', models.IntegerField(blank=True, null=True)),
                ('marca', models.CharField(blank=True, max_length=500, null=True)),
                ('potencia', models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True)),
                ('detalle', models.TextField(blank=True, null=True)),
                ('proyecto', models.ForeignKey(db_column='proyecto_id', on_delete=django.db.models.deletion.CASCADE, related_name='equipos', to='proyectos.proyecto')),
            ],
            options={
                'db_table': 'equipos',
            },
        ),
        migrations.AddConstraint(
            model_name='equipo',
            constraint=models.UniqueConstraint(fields=('proyecto', 'tipo'), name='uq_equipo_por_tipo'),
        ),
        migrations.AlterModelTable(
            name='proyectoinversor',
            table='inversores',
        ),
        migrations.AddField(
            model_name='proyectoinversor',
            name='equipo',
            field=models.ForeignKey(blank=True, db_column='equipo_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='inversores', to='proyectos.equipo'),
        ),
        migrations.AddField(
            model_name='proyectoinversor',
            name='cantidad_strings',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='proyectoinversor',
            name='marca',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name='proyectoinversor',
            name='estado',
            field=models.CharField(choices=[('activo', 'Activo'), ('retirado', 'Retirado')], default='activo', max_length=20),
        ),
        migrations.RunSQL(
            "ALTER TABLE inversores ALTER COLUMN estado SET DEFAULT 'activo';",
            "ALTER TABLE inversores ALTER COLUMN estado DROP DEFAULT;",
        ),
        migrations.RemoveField(
            model_name='proyectoinversor',
            name='activo',
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.DeleteModel(name='ProyectoInfoTecnica')],
        ),
    ]
