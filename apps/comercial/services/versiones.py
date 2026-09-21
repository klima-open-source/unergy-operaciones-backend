"""Las versiones de una oferta: reofertar sin borrar lo anterior.

**Reofertar no crea otra oferta: le agrega una versión.** La oferta conserva su
identidad —el consecutivo NUNCA cambia— y cada propuesta queda como una fila
más. Hasta ahora había un solo `documento_url` y un `precio_detalle` de texto
libre que se sobrescribían: la propuesta anterior desaparecía sin rastro. Ver
`docs/DOMINIO_COMERCIAL.md`, O-8 y O-9.

**Append-only.** No hay editar ni borrar una versión: corregir es agregar la
siguiente. Es el mismo patrón que la bitácora de Prospección, y de eso depende
que la cronología sea confiable.

**Qué guarda una versión, y qué no.** Solo lo que el CONTRATO necesita y la
oferta no tenía en ninguna parte: la tabla de precio por año, el índice de
indexación y el mes base. La modalidad, las garantías y la forma de pago son
idénticas en todas las ofertas revisadas y nada en la plataforma ramifica por
ellas — viven en el PDF, que está en Drive. Agregar campos que nadie lee es
justo lo que produjo `etapa_texto`: texto libre que se guarda, se devuelve y no
interpreta nadie.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Max

from api.exceptions import Conflict, NoProcesable
from apps.comercial.models import (
    OportunidadOferta, OportunidadOfertaVersion, OportunidadOfertaVersionPrecio,
)


def ultima(oferta: OportunidadOferta) -> OportunidadOfertaVersion | None:
    """La versión vigente: la de número más alto.

    Es de la que se derivan los estados de la oferta. Que sea la más alta y no
    la más reciente por fecha importa: una versión puede crearse hoy como
    borrador y enviarse la semana que viene.
    """
    return oferta.versiones.order_by("-numero").first()


def aceptada(oferta: OportunidadOferta) -> OportunidadOfertaVersion | None:
    """La versión que el cliente aceptó, si alguna. Es la que se firma."""
    return oferta.versiones.filter(fecha_aceptacion__isnull=False).first()


def agregar(oferta: OportunidadOferta, datos: dict, usuario=None
            ) -> OportunidadOfertaVersion:
    """La siguiente versión de la oferta, con su tabla de precios.

    El número se asigna acá y no lo manda el cliente: es el orden de la
    propuesta dentro de su oferta, no un dato que alguien elija. Se toma el
    máximo dentro de una transacción que bloquea la oferta, porque dos
    reofertas simultáneas leerían el mismo máximo; el único
    `(oferta, numero)` es la segunda red.
    """
    precios = datos.get("precios") or []
    anios = [p["anio"] for p in precios]
    if len(anios) != len(set(anios)):
        raise NoProcesable("la tabla de precios tiene años repetidos")

    with transaction.atomic():
        # Bloquea la fila de la oferta mientras se calcula el número.
        OportunidadOferta.objects.select_for_update().filter(pk=oferta.pk).first()
        siguiente = (
            oferta.versiones.aggregate(m=Max("numero"))["m"] or 0
        ) + 1

        version = OportunidadOfertaVersion.objects.create(
            oferta_id=oferta.pk,
            numero=siguiente,
            fecha_envio=datos.get("fecha_envio"),
            documento_url=datos.get("documento_url"),
            indice_indexacion=datos.get("indice_indexacion"),
            periodo_indexacion_base=datos.get("periodo_indexacion_base"),
            que_cambio=datos.get("que_cambio"),
            creado_por_usuario_id=getattr(usuario, "id", None),
        )
        if precios:
            OportunidadOfertaVersionPrecio.objects.bulk_create([
                OportunidadOfertaVersionPrecio(
                    version_id=version.id, anio=p["anio"], precio=p["precio"],
                )
                for p in precios
            ])

    return version


def aceptar(version: OportunidadOfertaVersion, fecha) -> OportunidadOfertaVersion:
    """Marca la versión que el cliente aceptó. Es la que se firmará.

    **Como mucho una por oferta.** Si el cliente después acepta otra propuesta,
    lo que hubo fue una negociación más: entra una versión nueva y se acepta
    ésa. Dejar dos aceptadas haría ambiguo con qué condiciones nace el contrato,
    que es justo lo que esta tabla viene a resolver.
    """
    ya = aceptada(version.oferta)
    if ya is not None and ya.pk != version.pk:
        raise Conflict(
            f"La oferta ya tiene aceptada la versión {ya.numero}. Para cambiar "
            "las condiciones, agrega una versión nueva y acepta ésa."
        )
    if version.fecha_envio is None:
        raise NoProcesable(
            "No se puede aceptar un borrador: la versión no tiene fecha de envío"
        )

    version.fecha_aceptacion = fecha
    version.save(update_fields=["fecha_aceptacion"])
    return version


def condiciones_para_contrato(version: OportunidadOfertaVersion) -> dict:
    """Lo que la propuesta aceptada le aporta al contrato.

    **El contrato nace de lo que se negoció, no de lo que alguien vuelva a
    teclear.** Antes el diálogo de firma preguntaba el precio, el indexador y el
    mes base porque la oferta no los tenía en ninguna parte: `precio_detalle` era
    texto libre. Ahora la versión los guarda, así que salen de ahí.

    **Las fechas NO están acá**, y es a propósito. El período de suministro vive
    en la oferta (`fecha_tentativa_inicio` / `fecha_fin_tentativa`) y es
    tentativo: la fecha real del contrato se confirma al firmar. Meterla en la
    versión sería modelar un dato que todavía no se negocia por propuesta.
    """
    precios = [
        {"anio": p.anio, "precio": float(p.precio)}
        for p in sorted(version.precios.all(), key=lambda x: x.anio)
    ]
    return {
        "precios_anuales": precios or None,
        "indice_indexacion": version.indice_indexacion,
        "periodo_indexacion_base": version.periodo_indexacion_base,
        "carpeta_link": version.documento_url,
    }
