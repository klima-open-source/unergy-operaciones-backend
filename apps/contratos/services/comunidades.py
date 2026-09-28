"""Comunidades energéticas: qué servicios deja de recibir una planta.

Unergy entró a ese negocio y se negocia **dentro del PPA**. La regla (Sara,
2026-09-18):

> Si un PPA entra a comunidad energética, a esas plantas **ya no se les presta
> representación ni CGM**.

Es la primera regla de exclusión del dominio: hasta acá todo era "tiene contrato
vigente → tiene el servicio", y esto agrega combinaciones que no deben existir.

**Nada se escribe en los contratos de representación.** Podrían marcarse
`terminado` al entrar la planta a la comunidad, pero entonces, al salir, no
habría forma de distinguir cuáles cerró el sistema y cuáles cerró una persona
--por una renegociación, por un cliente que se fue-- y reabrirlos todos reviviría
contratos que debían seguir cerrados. Calculando la exclusión, el contrato queda
"firmado pero no vigente" y, si la planta sale de la comunidad, sus servicios
vuelven solos. Es el mismo principio que `vigencia`: lo que depende del tiempo no
se guarda.

**La fecha importa.** La entrada a la comunidad NO coincide con el inicio del
PPA, así que va en `ppa_contratos.fecha_entrada_comunidad`. Sin ella la exclusión
sería "desde siempre", y un PPA que entra a comunidad en 2027 borraría los
servicios que sí se prestaron en 2026.

**Cómo usarlo sin volverlo lento.** `plantas_en_comunidad()` hace UNA consulta y
devuelve un conjunto de ids; `presta_servicio()` solo lo consulta en memoria.
Preguntarle a la base por cada contrato serían 160 consultas donde basta una --el
mismo N+1 que ya se evita con las plantas y los enlaces de Drive.
"""

from __future__ import annotations

from datetime import date

from django.db.models import Q

from apps.contratos.services import grupos, vigencia

#: Los subservicios que una planta en comunidad deja de recibir. Los de
#: Operación no entran: el O&M se sigue prestando aunque la planta esté en
#: comunidad -- lo que cambia es quién comercializa su energía.
EXCLUIDOS_EN_COMUNIDAD = frozenset({grupos.REPRESENTACION, grupos.CGM})


def plantas_en_comunidad(hoy: date) -> set[int]:
    """Los proyectos que hoy están en una comunidad energética. UNA consulta.

    Un PPA cuenta si está marcado como comunidad y su fecha de entrada ya llegó.
    Sin fecha, la exclusión aplica desde siempre: es un PPA que nació como
    comunidad y al que nadie le puso el dato.

    El PPA vencido no excluye nada: si el contrato de comunidad terminó, la
    planta vuelve a recibir representación y CGM.
    """
    from apps.ppa.models import PpaContrato, PpaContratoProyecto

    de_comunidad = PpaContrato.objects.filter(
        vigencia.filtro_ppa_vivos(hoy),
        deleted_at__isnull=True,
        es_comunidad_energetica=True,
    ).filter(
        Q(fecha_entrada_comunidad__isnull=True) | Q(fecha_entrada_comunidad__lte=hoy)
    )
    return set(
        PpaContratoProyecto.objects
        .filter(contrato__in=de_comunidad)
        .values_list("proyecto_id", flat=True)
    )


def presta_servicio(contrato, subservicio: str, hoy: date,
                    en_comunidad: set[int]) -> bool:
    """Si HOY se presta ese subservicio bajo ese contrato.

    Dos condiciones: que el contrato esté vivo (estado no cerrado y fecha que
    todavía rige) y que la exclusión por comunidad no aplique.

    `en_comunidad` se pasa ya resuelto, de `plantas_en_comunidad()`. No se
    consulta acá a propósito: esta función se llama una vez por contrato.
    """
    if not vigencia.esta_vivo(contrato, hoy):
        return False
    if subservicio in EXCLUIDOS_EN_COMUNIDAD and contrato.proyecto_id in en_comunidad:
        return False
    return True


def subservicios_prestados(contrato, hoy: date, en_comunidad: set[int]) -> list[str]:
    """Los subservicios del contrato que HOY se prestan de verdad.

    La diferencia con `grupos.subservicios_de()` es el "de verdad": esa dice qué
    cubre el contrato según sus tarifas, sin mirar si sigue vivo ni si la planta
    salió a una comunidad.
    """
    return [
        sub for sub in grupos.subservicios_de(contrato)
        if presta_servicio(contrato, sub, hoy, en_comunidad)
    ]


def motivo_bloqueo(proyecto_id: int | None, subservicio: str,
                   en_comunidad: set[int]) -> str | None:
    """Por qué NO se puede crear este contrato, o `None` si sí se puede.

    Existe para que la validación de la API y el mensaje que ve el usuario digan
    lo mismo, en vez de escribir el texto en dos sitios.
    """
    if subservicio not in EXCLUIDOS_EN_COMUNIDAD:
        return None
    if proyecto_id is None or proyecto_id not in en_comunidad:
        return None
    return (
        "La planta está en una comunidad energética, así que no se le presta "
        "representación ni CGM. Si eso cambió, primero hay que ajustar el PPA "
        "de comunidad."
    )
