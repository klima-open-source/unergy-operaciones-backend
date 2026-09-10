"""La potencia AC pasa a vivir en una sola columna, con su nombre real.

Dos problemas de una:

**El nombre mentía.** `proyectos.potencia_instalada_kwp` decía kWp (pico, DC) y
siempre guardó potencia AC. No es una sutileza: ya causó dos bugs de corrupción
--la edición manual de Información técnica copiaba ahí la capacidad DC (fix del
2026-08-19) y el backfill de Solenium hacía lo mismo-- y en los dos casos hubo
que arreglar datos, no solo código.

**Y estaba duplicada.** `proyecto_info_tecnica.potencia_ac_kw` guardaba el mismo
número, espejado a mano en dos lugares del código. Verificado en producción el
2026-09-10: coinciden en 101 de 101 proyectos, porque una se copia de la otra.

Sobrevive la de `proyectos`, renombrada. Por qué esa y no la otra:

  - La fila de `proyecto_info_tecnica` se crea A DEMANDA (`get_or_create`), así
    que si la potencia viviera solo ahí, una planta sin esa fila no tendría
    potencia en ninguna parte.
  - 66 lugares la leen desde `proyectos`; desde `info_tecnica` cada listado
    necesitaría atravesar la relación (un LEFT JOIN más y riesgo de N+1).
  - Los tres flujos que la escriben (`tsf_sync`, `sync_desde_json` y el sembrado
    desde un candidato pendiente) ya escriben en `proyectos`. Con una sola
    columna el espejo desaparece: una escritura, nada que pueda desincronizarse.

`RenameField` y no un remove+add: renombrar CONSERVA los datos de las 101 filas;
la pareja remove+add los borraría. `makemigrations` no lo puede adivinar solo
(pregunta de forma interactiva y el default es "no"), así que esta migración se
escribió a mano.

La capacidad PICO (DC) no se toca: sigue siendo
`proyecto_info_tecnica.capacidad_instalada_kwp`, que es el único lugar donde
estuvo siempre.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('proyectos', '0004_eliminar_promotor'),
    ]

    operations = [
        migrations.RenameField(
            model_name='proyecto',
            old_name='potencia_instalada_kwp',
            new_name='potencia_ac_kw',
        ),
        migrations.RemoveField(
            model_name='proyectoinfotecnica',
            name='potencia_ac_kw',
        ),
    ]
