"""PPAs de Operaciones unidos con los de originabot (`investment.PPA`).

Operaciones crea y configura el PPA; originabot tiene que saber qué minigranjas
cubre. Cada vez que cambian las plantas de un PPA, el cambio se lleva a
originabot:

- **Operaciones manda: la lista se REEMPLAZA.** Tras sincronizar, el PPA de
  originabot tiene exactamente las minigranjas de las plantas del contrato; lo que
  hubiera allá y aquí no esté, se quita. (PATCH de `minifarms`, que ya es la lista
  entera.)
- **Si no llega, queda pendiente.** Con originabot caído (o rechazando al usuario
  de Operaciones), el PPA queda `error` (`originabot_pendiente` lo marca) y el
  próximo intento vuelve a calcular la lista desde las plantas de ese momento.
- **Crear un PPA en originabot nunca es automático**: solo con `crear=True`, que
  manda la ficha del PPA después de que la persona confirma («Esta acción creará
  el PPA en TSF…»). Un PPA sin vínculo al que se le asignan plantas queda
  `pendiente` hasta que alguien lo cree o lo vincule. Se crea con todas sus
  plantas y un `off_taker` (obligatorio allá): el elegido, o el del comprador
  (`Cliente.originabot_offtaker_id`, o por nombre). **O se vincula a mano** a uno
  que ya existe allá: también se le REEMPLAZAN las minigranjas por las de aquí.
- La minigranja de una planta es `Proyecto.originabot_minifarm_id`; si falta, se
  resuelve una vez y se guarda: la que se llame exactamente como su
  `origina_code` o, si no hay, la ÚNICA que encuentre su código TSF (originabot
  renombró las antiguas: `COLCORT7P1` es «Minigranja 0030 - Chimá Oriente», pero
  su búsqueda sigue indexando el código). Con más de una candidata no se adivina.
  Una planta sin minigranja en originabot no se lleva y se avisa en el detalle:
  como la lista se reemplaza, esa planta no queda en el PPA de allá.

Las llamadas van con el usuario de Operaciones en originabot, sea quien sea el
que hizo el cambio (`apps/comun/integraciones/originabot.py`). Que falle NO deshace el cambio en
Operaciones: aquí se crea y se configura el PPA.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.clientes.models import Cliente
from apps.comun.integraciones.originabot import ClienteOriginabot, OriginabotError  # noqa: F401 (las vistas los usan desde aquí)
from apps.comun.nombre_matching import normalizar
from apps.contratos.models import Contrato, ContratoProyecto
from apps.proyectos.models import Proyecto
from apps.proyectos.services import originabot_minigranjas as minigranjas

logger = logging.getLogger(__name__)

SINCRONIZADO = "sincronizado"
PENDIENTE = "pendiente"
ERROR = "error"

SIN_VINCULO = (
    "Este PPA no existe en originabot: créalo o vincúlalo a uno de allá para que "
    "sus plantas lleguen."
)
SIN_OFFTAKER = (
    "Para crear este PPA en originabot falta su off taker (el comprador): elígelo o "
    "vincula el PPA a uno que ya exista allá."
)


def plantas_de(contrato) -> set[int]:
    return set(
        ContratoProyecto.objects.filter(contrato_id=contrato.pk).values_list("proyecto_id", flat=True)
    )


def registrar_cambio(contrato, antes: set[int], despues: set[int]) -> None:
    """Marca que las plantas cambiaron y originabot aún no se enteró. No hace
    falta saber qué cambió: la sincronización reemplaza la lista entera."""
    if antes == despues:
        return
    contrato.originabot_pendiente = {"reemplazar": True}
    Contrato.objects.filter(pk=contrato.pk).update(originabot_pendiente=contrato.originabot_pendiente)


def _marcar(contrato, estado: str, detalle: str | None, **extra) -> None:
    valores = {
        "originabot_sync_estado": estado,
        "originabot_sync_detalle": detalle,
        "originabot_sync_at": timezone.now(),
        **extra,
    }
    for campo, valor in valores.items():
        setattr(contrato, campo, valor)
    Contrato.objects.filter(pk=contrato.pk).update(**valores)


def _offtaker(contrato, ob: ClienteOriginabot, elegido: int | None) -> int | None:
    """El off taker del PPA: el elegido, el ya vinculado al comprador, o el del
    catálogo de originabot cuyo nombre está contenido en el del comprador."""
    comprador = Cliente.objects.filter(pk=contrato.comprador_id).first() if contrato.comprador_id else None
    if elegido:
        if comprador and not comprador.originabot_offtaker_id:
            Cliente.objects.filter(pk=comprador.pk).update(originabot_offtaker_id=elegido)
        return elegido
    if comprador is None:
        return None
    if comprador.originabot_offtaker_id:
        return comprador.originabot_offtaker_id
    palabras = set(normalizar(comprador.razon_social_nombre).split())
    candidatos = [
        o["id"] for o in ob.offtakers()
        if normalizar(o["nombre"]) and set(normalizar(o["nombre"]).split()) <= palabras
    ]
    if len(candidatos) != 1:
        return None
    Cliente.objects.filter(pk=comprador.pk).update(originabot_offtaker_id=candidatos[0])
    return candidatos[0]


def _nombre(contrato) -> str:
    return contrato.nombre_interno or contrato.numero_codigo_contrato or f"PPA Operaciones #{contrato.pk}"


def sincronizar(
    contrato, *, vincular_a: int | None = None,
    off_taker_id: int | None = None, crear: bool = False,
) -> None:
    """Lleva a originabot lo pendiente del PPA y deja el resultado en el contrato
    (`originabot_sync_*`). No lanza: un fallo queda como `error` con su motivo."""
    ob = ClienteOriginabot()
    try:
        if vincular_a:
            ob.ppa(vincular_a)  # que exista; 404 → error legible
            otro = (
                Contrato.objects.filter(originabot_ppa_id=vincular_a, deleted_at__isnull=True)
                .exclude(pk=contrato.pk).first()
            )
            if otro:
                _marcar(contrato, ERROR, f"Ese PPA de originabot ya está vinculado al contrato #{otro.pk}.")
                return
            Contrato.objects.filter(pk=contrato.pk).update(originabot_ppa_id=vincular_a)
            contrato.originabot_ppa_id = vincular_a
        elif contrato.originabot_ppa_id is None and not crear:
            # Crear allá exige una confirmación explícita: aquí solo se avisa.
            if plantas_de(contrato):
                _marcar(contrato, PENDIENTE, SIN_VINCULO)
            return

        # La lista que debe quedar allá: la minigranja de cada planta del contrato.
        proyectos = Proyecto.objects.in_bulk(plantas_de(contrato))
        objetivo, sin_minigranja = set(), []
        for pid in sorted(proyectos):
            minifarm = minigranjas.minigranja_de(proyectos[pid], ob)
            if minifarm is None:
                sin_minigranja.append(proyectos[pid].nombre_comercial)
            else:
                objetivo.add(minifarm)

        if contrato.originabot_ppa_id is None:
            off_taker = _offtaker(contrato, ob, off_taker_id)
            if off_taker is None:
                _marcar(contrato, PENDIENTE, SIN_OFFTAKER)
                return
            creado = ob.crear_ppa(_nombre(contrato), off_taker, sorted(objetivo))
            Contrato.objects.filter(pk=contrato.pk).update(originabot_ppa_id=creado["id"])
            contrato.originabot_ppa_id = creado["id"]
        else:
            actual = set(ob.ppa(contrato.originabot_ppa_id).get("minifarms") or [])
            if actual != objetivo:
                ob.fijar_minifarms(contrato.originabot_ppa_id, sorted(objetivo))
    except OriginabotError as exc:
        _marcar(contrato, ERROR, str(exc))
        return

    detalle = (
        "Sin minigranja en originabot, no se llevaron: " + ", ".join(sin_minigranja)
        if sin_minigranja else None
    )
    _marcar(contrato, SINCRONIZADO, detalle, originabot_pendiente=None)
