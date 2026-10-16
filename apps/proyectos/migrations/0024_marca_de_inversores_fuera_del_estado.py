"""La marca de los inversores pasa al equipo de inversores (Sara, 2026-10-16).

    inversores.marca  ->  equipos.marca (el equipo de tipo inversor de la planta)

Todos los inversores de una planta son de la misma marca; la potencia sí cambia
y se queda en cada inversor.

Solo estado: la columna sigue en la base, sin leerse ni escribirse, hasta que
`pasar_marca_inversores_a_equipo --aplicar` copie las marcas y un despliegue
posterior la borre. Es nullable: crear un inversor sin ella no falla.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('proyectos', '0023_borrar_portafolio_proyectos_e_info_tecnica'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.RemoveField(model_name='proyectoinversor', name='marca')],
        ),
    ]
