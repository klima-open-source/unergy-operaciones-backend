"""Borra `portafolio_proyectos` y `proyecto_info_tecnica` (Sara, 2026-10-16). IRREVERSIBLE.

Las dos salieron del modelo en `0020_portafolio_unico` y `0022_equipos`; sus datos
se copiaron en producción el 2026-10-16, a mano y una vez:

- `volver_a_portafolio_unico --aplicar`: `portafolio_proyectos` ->
  `proyectos.portafolio_id`. Ninguna planta cambió.
- `pasar_info_tecnica_a_equipos --aplicar`: `proyecto_info_tecnica` -> `proyectos`
  y `equipos` (371 equipos, 675 inversores enlazados).

Lo que la copia no llevó se pierde, por decisión de Sara: `url_ubicacion` (72
plantas), `potencia_inversores_kwp` (30), `tiene_internet` (14) y la potencia de
panel de Perijá, La Paz Vallenata y Baraya, que no era un número («0.575, 0.580»)
y se corrige a mano en la ficha.

Ninguna tabla las referencia por clave foránea. Respaldo de la base tomado por
Sara antes de la subida 3, el mismo día.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('proyectos', '0022_equipos'),
    ]

    operations = [
        # Sin reverse_sql: no hay vuelta atrás por migración, solo por el respaldo.
        migrations.RunSQL(
            "DROP TABLE IF EXISTS portafolio_proyectos;"
            "DROP TABLE IF EXISTS proyecto_info_tecnica;"
        ),
    ]
