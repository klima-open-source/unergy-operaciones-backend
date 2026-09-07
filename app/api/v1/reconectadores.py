"""Reconectadores — estado y comandos ON/OFF del relay via Solenium."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.v1.auth import get_current_user
from app.core.database import get_db
from app.models.proyectos import Proyecto, TipoProyectoEnum
from app.services.mgs.solenium_client import SoleniumClient

logger = logging.getLogger("reconectadores")
router = APIRouter(prefix="/reconectadores", tags=["Reconectadores"])

# Solenium migro de solenium.co a sole.tech (2026-09-07); el dominio viejo
# esta muerto. La version viva de este modulo es
# apps/monitoreo/services/reconectadores.py, que ya las toma de
# configuracion -- estas se actualizan para que los dos arboles no digan
# cosas distintas si alguien compara.
_SOLENIUM_AUTH_URL  = "https://auth.sole.tech/api/token/"
_SOLENIUM_RELAY_SET = "https://data.sole.tech/api/project/{sol_id}/relay/set-status/"
_SOLENIUM_RELAY_GET = "https://data.sole.tech/api/project/{sol_id}/relay/"

# Cliente interno (usa credenciales de Railway — no necesita creds del usuario)
_client: SoleniumClient | None = None

def _get_client() -> SoleniumClient:
    global _client
    if _client is None:
        _client = SoleniumClient()
    if not _client.enabled:
        raise HTTPException(503, "Solenium no configurado en el servidor")
    return _client


# ── Schemas ────────────────────────────────────────────────────────────────────

class ComandoRequest(BaseModel):
    username:         str
    password:         str
    accion:           Literal["ON", "OFF"]
    is_interrogating: bool = True


class ComandoResponse(BaseModel):
    success: bool
    message: str
    accion:  str
    detail:  str | None = None


class RelayEstado(BaseModel):
    proyecto_id: int
    nombre:      str
    sol_id:      int
    active:      bool | None   # True=ON, False=OFF, None=sin dato

    # Telemetría en vivo del relay — las mismas columnas del panel
    # "Reconectadores" de Solenium.
    corriente_a:          float | None = None   # I_A
    corriente_b:          float | None = None   # I_B
    corriente_c:          float | None = None   # I_C
    corriente_n:          float | None = None   # I_N
    voltaje_a:            float | None = None   # U_A
    voltaje_b:            float | None = None   # U_B
    voltaje_c:            float | None = None   # U_C
    voltaje_r:            float | None = None   # U_R
    voltaje_s:            float | None = None   # U_S
    voltaje_t:            float | None = None   # U_T
    frecuencia_hz:        float | None = None   # F_ABC
    reactiva_kva:         float | None = None   # Reactiva
    potencia_kw:          float | None = None   # Activa
    factor_potencia:      float | None = None   # PF
    ultima_actualizacion: str | None = None     # Tiempo


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_user_token(username: str, password: str) -> str:
    """Autentica con credenciales del usuario y retorna JWT."""
    try:
        with httpx.Client(timeout=15) as c:
            resp = c.post(_SOLENIUM_AUTH_URL,
                          json={"username": username, "password": password})
    except Exception as exc:
        raise HTTPException(503, f"No se pudo conectar a Solenium: {exc}") from exc

    if resp.status_code == 401:
        raise HTTPException(401, "Credenciales Solenium incorrectas")
    if resp.status_code not in (200, 201):
        raise HTTPException(502, f"Solenium auth → HTTP {resp.status_code}")

    token = resp.json().get("access")
    if not token:
        raise HTTPException(502, "Solenium no devolvió token")
    return token


def _num(v) -> float | None:
    """Solenium a veces manda las medidas como texto o como null."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_relay_estado(sol_id: int, client: SoleniumClient) -> tuple[bool, dict]:
    """
    Llama GET /project/{sol_id}/relay/.
    Respuesta Solenium: {"results": {"active": true|false|null, "u_a": ..., "kw": ...},
    "success": true}

    Retorna (tiene_reconectador, results):
      - tiene_reconectador=False: Solenium respondió 404 (sin relay físico) o hubo
        un error/timeout (no se pudo confirmar) → el proyecto debe omitirse.
      - tiene_reconectador=True: Solenium confirmó el relay; `results` trae el
        estado (`active`) y la telemetría eléctrica, que puede venir incompleta.
    """
    url = _SOLENIUM_RELAY_GET.format(sol_id=sol_id)
    try:
        data = client._get(url)   # retorna None en 404 o en error/timeout
        if not data:
            return False, {}
        return True, (data.get("results") or {})
    except Exception as exc:
        logger.warning("relay_get sol_id=%d error=%s", sol_id, exc)
        return False, {}


def _build_estado(proyecto_id: int, nombre: str, sol_id: int, r: dict) -> RelayEstado:
    """Traduce el `results` de Solenium a la telemetría que consume el móvil."""
    ts = r.get("time")
    return RelayEstado(
        proyecto_id=proyecto_id,
        nombre=nombre,
        sol_id=sol_id,
        active=r.get("active"),
        corriente_a=_num(r.get("i_a")),
        corriente_b=_num(r.get("i_b")),
        corriente_c=_num(r.get("i_c")),
        corriente_n=_num(r.get("i_n")),
        voltaje_a=_num(r.get("u_a")),
        voltaje_b=_num(r.get("u_b")),
        voltaje_c=_num(r.get("u_c")),
        voltaje_r=_num(r.get("u_r")),
        voltaje_s=_num(r.get("u_s")),
        voltaje_t=_num(r.get("u_t")),
        frecuencia_hz=_num(r.get("f_abc")),
        reactiva_kva=_num(r.get("kva")),
        potencia_kw=_num(r.get("kw")),
        factor_potencia=_num(r.get("pf")),
        ultima_actualizacion=str(ts) if ts is not None else None,
    )


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/debug-relay/{proyecto_id}")
def debug_relay(
    proyecto_id: int,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Debug: muestra la respuesta raw de Solenium para el relay de un proyecto."""
    client = _get_client()
    proyecto = db.query(Proyecto).filter(Proyecto.id == proyecto_id).first()
    if not proyecto or not proyecto.project_id_solenium:
        raise HTTPException(404, "Proyecto sin sol_id")
    sol_id = int(proyecto.project_id_solenium)
    url = _SOLENIUM_RELAY_GET.format(sol_id=sol_id)
    raw = client._get(url)
    tiene_reconectador, results = _fetch_relay_estado(sol_id, client)
    return {"sol_id": sol_id, "url": url, "raw": raw,
            "tiene_reconectador": tiene_reconectador,
            "parsed": _build_estado(proyecto.id, proyecto.nombre_comercial,
                                    sol_id, results) if tiene_reconectador else None}


@router.get("/estados", response_model=list[RelayEstado])
def get_estados(
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """
    Retorna el estado real del relay (`active`) y su telemetría eléctrica
    (corrientes, voltajes, frecuencia, reactiva, activa y PF — las mismas
    columnas del panel "Reconectadores" de Solenium) para cada proyecto,
    consultando directamente Solenium con las credenciales del servidor.
    No requiere credenciales del usuario.
    """
    client = _get_client()

    proyectos = db.query(Proyecto).filter(
        Proyecto.estado == "en_operacion",
        Proyecto.project_id_solenium.isnot(None),
        Proyecto.tipo_proyecto == TipoProyectoEnum.minigranja,
        Proyecto.srv_operacion == True,  # noqa: E712
    ).all()

    if not proyectos:
        return []

    def _query(p) -> RelayEstado | None:
        try:
            sol_id = int(p.project_id_solenium)
        except (TypeError, ValueError):
            logger.warning("project_id_solenium inválido proyecto_id=%s valor=%r",
                           p.id, p.project_id_solenium)
            return None
        tiene_reconectador, results = _fetch_relay_estado(sol_id, client)
        if not tiene_reconectador:
            return None
        return _build_estado(p.id, p.nombre_comercial, sol_id, results)

    with ThreadPoolExecutor(max_workers=8) as ex:
        resultados = [r for r in ex.map(_query, proyectos) if r is not None]

    return resultados


@router.post("/{proyecto_id}/comando", response_model=ComandoResponse)
def enviar_comando(
    proyecto_id: int,
    body: ComandoRequest,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """
    Autentica con Solenium usando las credenciales del body y envía
    un comando ON/OFF al relay de la planta.
    Las credenciales se validan en cada llamada — nunca se almacenan.
    """
    proyecto = db.query(Proyecto).filter(Proyecto.id == proyecto_id).first()
    if not proyecto:
        raise HTTPException(404, "Proyecto no encontrado")
    if not proyecto.project_id_solenium:
        raise HTTPException(422, "Este proyecto no tiene ID de Solenium configurado")

    sol_id = int(proyecto.project_id_solenium)
    token  = _get_user_token(body.username, body.password)

    url = _SOLENIUM_RELAY_SET.format(sol_id=sol_id)
    try:
        with httpx.Client(timeout=30) as c:
            resp = c.post(
                url,
                json={"status_to_set": body.accion,
                      "is_interrogating": body.is_interrogating},
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:
        raise HTTPException(503, f"Error de conexión: {exc}") from exc

    logger.info("relay_cmd proyecto=%d sol_id=%d accion=%s user=%s http=%s",
                proyecto_id, sol_id, body.accion, body.username, resp.status_code)

    if resp.status_code < 300:
        return ComandoResponse(
            success=True,
            message=f"Comando {body.accion} enviado a {proyecto.nombre_comercial}",
            accion=body.accion,
            detail=resp.text[:200],
        )

    raise HTTPException(502,
        detail=f"Solenium → HTTP {resp.status_code}: {resp.text[:120]}")
