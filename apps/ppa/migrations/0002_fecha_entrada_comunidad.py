"""La comunidad energética se guarda en el PPA: desde cuándo, y cuál es.

Unergy entró al negocio de comunidades energéticas, y eso se negocia dentro del
PPA. La regla (Sara, 2026-09-18): **a una planta en comunidad ya no se le presta
representación ni CGM**.

`es_comunidad_energetica` ya existía y dice QUÉ es el PPA. Falta el CUÁNDO: la
entrada a la comunidad **no coincide con el inicio del PPA** --confirmado con
negocio-- así que no se puede derivar de `fecha_inicio`. Sin esta fecha, la
exclusión sería "desde siempre", y un PPA que entra a comunidad en 2027 borraría
los servicios que sí se prestaron en 2026.

Va en el contrato y no en la planta porque ahí es donde se negocia -- el mismo
criterio que se aplicó a las banderas `srv_*`: el dato vive donde se decide.

**Nada se termina ni se escribe en los contratos de representación.** La
exclusión se calcula al leer (`apps.contratos.services.grupos.presta_servicio`),
así que el contrato sigue diciendo `firmado` y, si la planta sale de la
comunidad, sus servicios vuelven solos. Escribir `terminado` habría obligado a
distinguir después qué cerró el sistema y qué cerró una persona.

Nula mientras el PPA no sea de comunidad. Nula EN un PPA de comunidad significa
que la exclusión aplica desde siempre: es el caso de un PPA que nace como
comunidad y al que nadie le puso la fecha.

**`nombre_comunidad` se muda acá desde `proyectos`.** Estaba en la planta, así
que cinco plantas de la misma comunidad guardaban el nombre cinco veces y nada
garantizaba que lo escribieran igual. Si la comunidad se negocia en el PPA, su
nombre pertenece al PPA. La columna vieja se elimina en
`proyectos/0008_quitar_comunidad_del_proyecto`.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("ppa", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="ppacontrato",
            name="fecha_entrada_comunidad",
            field=models.DateField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name="ppacontrato",
            name="nombre_comunidad",
            field=models.CharField(max_length=255, null=True, blank=True),
        ),
    ]
