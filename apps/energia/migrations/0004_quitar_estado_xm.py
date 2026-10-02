# El panel "Estado en Quoia" (aprobación de XM sobre lo enviado) se reemplazó
# por el resumen del envío (envio.resumen_envio), decisión de Sara 2026-10-02.
# Estas cuatro columnas solo las escribía y leía ese panel.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('energia', '0003_curva_cgm_referencia'),
    ]

    operations = [
        migrations.RemoveField(model_name='reporteenergiageneracion', name='xm_process_id'),
        migrations.RemoveField(model_name='reporteenergiageneracion', name='xm_estado'),
        migrations.RemoveField(model_name='reporteenergiageneracion', name='xm_exitoso'),
        migrations.RemoveField(model_name='reporteenergiageneracion', name='xm_verificado_en'),
        migrations.RemoveField(model_name='reporteenergiaconsumo', name='xm_process_id'),
        migrations.RemoveField(model_name='reporteenergiaconsumo', name='xm_estado'),
        migrations.RemoveField(model_name='reporteenergiaconsumo', name='xm_exitoso'),
        migrations.RemoveField(model_name='reporteenergiaconsumo', name='xm_verificado_en'),
    ]
