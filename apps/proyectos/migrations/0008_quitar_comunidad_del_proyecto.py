"""La comunidad energética deja de vivir en la planta: se decide en el PPA.

    proyectos.es_comunidad_energetica   ->  se deriva de ppa_contratos
    proyectos.nombre_comunidad          ->  ppa_contratos.nombre_comunidad

Eran el mismo patrón que las banderas `srv_*`: un dato que alguien marcaba a
mano en la ficha de la planta --con un interruptor en `ProyectoDetailView`-- y
que nada sincronizaba con el PPA, donde la comunidad se negocia de verdad.

El propio código ya había empezado a corregirlo por su cuenta:
`apps/comercial/services/pipeline.py` tenía

    if ppa is not None and ppa.es_comunidad_energetica is not None:
        return bool(ppa.es_comunidad_energetica)

es decir, alguien ya decidió que el PPA manda. Pero solo ahí; el resto seguía
leyendo el campo de la planta.

`nombre_comunidad` no se podía derivar --el PPA no tenía dónde guardarlo-- así
que se mudó en `ppa/0002`. Tampoco era su sitio: cinco plantas de la misma
comunidad guardaban cinco copias del nombre.

**La guarda no es adorno.** Negocio confirmó el 2026-09-18 que todavía no hay
comunidades registradas, pero no se pudo verificar contra la base --el acceso
estaba caído ese día--. Si al desplegar hay aunque sea una fila marcada, esta
migración FALLA y el deploy se detiene, en vez de borrar un dato que nadie
podría recuperar. Mismo patrón que la revisión 125 de Alembic.

Si falla: los datos hay que pasarlos al PPA de comunidad correspondiente antes
de volver a intentar.
"""

from django.db import migrations


def verificar_que_no_hay_datos(apps, schema_editor):
    """Se detiene si alguna planta tiene la comunidad marcada o nombrada."""
    Proyecto = apps.get_model("proyectos", "Proyecto")
    marcadas = Proyecto.objects.filter(es_comunidad_energetica=True).count()
    nombradas = (
        Proyecto.objects
        .exclude(nombre_comunidad__isnull=True)
        .exclude(nombre_comunidad="")
        .count()
    )
    if marcadas or nombradas:
        raise RuntimeError(
            f"No se pueden borrar las columnas de comunidad: hay {marcadas} "
            f"planta(s) con `es_comunidad_energetica=True` y {nombradas} con "
            f"`nombre_comunidad`. Se esperaba 0 de cada una.\n"
            f"Pasa esos datos al PPA de comunidad "
            f"(`es_comunidad_energetica`, `fecha_entrada_comunidad`, "
            f"`nombre_comunidad`) y vuelve a desplegar."
        )


def sin_reversa(apps, schema_editor):
    """Al revertir no hay nada que restaurar: se comprobó que estaban vacías."""


class Migration(migrations.Migration):

    dependencies = [
        ("proyectos", "0007_quitar_indice_solarview_repetido"),
        # El nombre tiene que existir en el PPA antes de borrarlo de la planta.
        ("ppa", "0002_fecha_entrada_comunidad"),
    ]

    operations = [
        migrations.RunPython(verificar_que_no_hay_datos, sin_reversa),
        migrations.RemoveField(model_name="proyecto", name="es_comunidad_energetica"),
        migrations.RemoveField(model_name="proyecto", name="nombre_comunidad"),
    ]
