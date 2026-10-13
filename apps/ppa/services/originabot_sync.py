"""PPAs de Operaciones unidos con los de originabot (`investment.PPA`).

Operaciones crea y configura el PPA; originabot tiene que saber qué minigranjas
cubre. Cada vez que cambian las plantas de un PPA, el cambio se lleva a
originabot:

- **Solo el cambio, nunca la lista entera.** Se agregan las minigranjas de las
  plantas que entraron y se quitan las de las que salieron; lo que originabot
  tenga y Operaciones no conozca (una minigranja sin planta aquí) no se toca.
  originabot solo acepta la lista completa (PATCH de `minifarms`), así que se lee
  la actual, se le aplica el cambio y se escribe.
- **El cambio se acumula en `originabot_pendiente` hasta que llegue.** Si no se
  pudo llevar —la persona entró con el login propio y no hay token de Unergy, u
  originabot falló—, el PPA queda `pendiente`/`error` y el próximo intento lleva
  todo lo acumulado.
- **Un PPA sin vínculo se crea en originabot** con todas sus plantas, la primera
  vez que tiene alguna. originabot exige un `off_taker`: es el comprador del PPA,
  vinculado por `Cliente.originabot_offtaker_id`; si no se puede resolver, el PPA
  queda pendiente hasta que alguien lo elija. **O se vincula a mano** a uno que ya
  existe allá: entonces solo se le AGREGAN las plantas actuales.
- La minigranja de una planta es `Proyecto.originabot_minifarm_id`; si falta, se
  resuelve una vez y se guarda: la que se llame exactamente como su
  `origina_code` o, si no hay, la ÚNICA que encuentre su código TSF (originabot
  renombró las antiguas: `COLCORT7P1` es «Minigranja 0030 - Chimá Oriente», pero
  su búsqueda sigue indexando el código). Con más de una candidata no se adivina.
  Una planta sin minigranja en originabot no se lleva y se avisa en el detalle.

Las llamadas van con el token de Unergy Auth de quien hizo el cambio
(`apps/comun/integraciones/originabot.py`). Que falle NO deshace el cambio en
Operaciones: aquí se crea y se configura el PPA.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError
from django.utils import timezone

from apps.clientes.models import Cliente
from apps.comun.integraciones.originabot import ClienteOriginabot, OriginabotError  # noqa: F401 (las vistas los usan desde aquí)
from apps.comun.nombre_matching import normalizar
from apps.contratos.models import Contrato, ContratoProyecto
from apps.proyectos.models import Proyecto
from apps.proyectos.services.tsf_sync import _tsf_code_from_base_name

logger = logging.getLogger(__name__)

SINCRONIZADO = "sincronizado"
PENDIENTE = "pendiente"
ERROR = "error"

SIN_TOKEN = (
    "El cambio no llegó a originabot: entra con Unergy Auth y pulsa «Sincronizar»."
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
    """Acumula en `originabot_pendiente` lo que cambió. Una planta que entra y
    sale antes de sincronizar se cancela: originabot nunca la vio."""
    if antes == despues:
        return
    pendiente = contrato.originabot_pendiente or {}
    por_agregar = set(pendiente.get("agregar", []))
    por_quitar = set(pendiente.get("quitar", []))
    entran, salen = despues - antes, antes - despues
    # Lo que se iba a agregar y sale, o se iba a quitar y vuelve, se cancela: el
    # estado de originabot para esa planta no cambió. Sin esto, una planta que
    # entra y sale antes de sincronizar terminaría QUITANDO allá una minigranja
    # que alguien sí tenía puesta.
    agregar = (por_agregar - salen) | (entran - por_quitar)
    quitar = (por_quitar - entran) | (salen - por_agregar)
    contrato.originabot_pendiente = (
        {"agregar": sorted(agregar), "quitar": sorted(quitar)} if agregar or quitar else None
    )
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


def _minifarm_de(proyecto: Proyecto, ob: ClienteOriginabot) -> int | None:
    if proyecto.originabot_minifarm_id:
        return proyecto.originabot_minifarm_id
    minifarm = None
    if proyecto.origina_code:
        exactas = [m for m in ob.buscar_minifarms(proyecto.origina_code) if m.get("name") == proyecto.origina_code]
        if len(exactas) == 1:
            minifarm = exactas[0]["id"]
    if minifarm is None:
        codigo = proyecto.codigo_tsf or _tsf_code_from_base_name(proyecto.origina_code)
        candidatas = ob.buscar_minifarms(codigo) if codigo else []
        if len(candidatas) == 1:
            minifarm = candidatas[0]["id"]
    if minifarm is None:
        return None
    try:
        Proyecto.objects.filter(pk=proyecto.pk).update(originabot_minifarm_id=minifarm)
    except IntegrityError:
        # Otra planta viva ya tiene esa minigranja: es un duplicado que una persona
        # tiene que resolver; aquí no se adivina cuál es la buena.
        logger.warning("Minigranja %s ya asignada a otra planta (proyecto %s)", minifarm, proyecto.pk)
        return None
    return minifarm


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
    contrato, token: str | None, *, vincular_a: int | None = None, off_taker_id: int | None = None,
) -> None:
    """Lleva a originabot lo pendiente del PPA y deja el resultado en el contrato
    (`originabot_sync_*`). No lanza: un fallo queda como `error` con su motivo."""
    if not token:
        hay_algo_que_llevar = (
            bool(contrato.originabot_pendiente)
            or bool(vincular_a)
            or (contrato.originabot_ppa_id is None and bool(plantas_de(contrato)))
        )
        if hay_algo_que_llevar:
            _marcar(contrato, PENDIENTE, SIN_TOKEN)
        return

    ob = ClienteOriginabot(token)
    pendiente = contrato.originabot_pendiente or {}
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
            # Vincular a uno existente solo AGREGA: lo que ya tenga allá se respeta.
            agregar, quitar = plantas_de(contrato), set()
        elif contrato.originabot_ppa_id is None:
            agregar, quitar = plantas_de(contrato), set()
            if not agregar:
                return  # sin plantas no hay nada que crear todavía
        else:
            agregar, quitar = set(pendiente.get("agregar", [])), set(pendiente.get("quitar", []))
            if not agregar and not quitar:
                _marcar(contrato, SINCRONIZADO, contrato.originabot_sync_detalle)
                return

        proyectos = Proyecto.objects.in_bulk(agregar | quitar)
        mf_agregar, mf_quitar, sin_minigranja = set(), set(), []
        for pid in sorted(agregar | quitar):
            proyecto = proyectos.get(pid)
            minifarm = _minifarm_de(proyecto, ob) if proyecto else None
            if minifarm is None:
                sin_minigranja.append(proyecto.nombre_comercial if proyecto else f"#{pid}")
            elif pid in agregar:
                mf_agregar.add(minifarm)
            else:
                mf_quitar.add(minifarm)

        if contrato.originabot_ppa_id is None:
            off_taker = _offtaker(contrato, ob, off_taker_id)
            if off_taker is None:
                _marcar(contrato, PENDIENTE, SIN_OFFTAKER)
                return
            creado = ob.crear_ppa(_nombre(contrato), off_taker, sorted(mf_agregar))
            Contrato.objects.filter(pk=contrato.pk).update(originabot_ppa_id=creado["id"])
            contrato.originabot_ppa_id = creado["id"]
        else:
            actual = set(ob.ppa(contrato.originabot_ppa_id).get("minifarms") or [])
            nuevo = (actual - mf_quitar) | mf_agregar
            if nuevo != actual:
                ob.fijar_minifarms(contrato.originabot_ppa_id, sorted(nuevo))
    except OriginabotError as exc:
        _marcar(contrato, ERROR, str(exc))
        return

    detalle = (
        "Sin minigranja en originabot, no se llevaron: " + ", ".join(sin_minigranja)
        if sin_minigranja else None
    )
    _marcar(contrato, SINCRONIZADO, detalle, originabot_pendiente=None)
