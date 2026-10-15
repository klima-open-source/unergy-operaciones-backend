"""Cliente HTTP de originabot (TSF), con el usuario de Operaciones.

Operaciones llama a originabot SIEMPRE como el user «operaciones» de Unergy Auth
(`ORIGINABOT_USER` / `ORIGINABOT_PASSWORD`), nunca con el token de quien está en
sesión: así funciona igual para quien entró con el login propio de Operaciones,
y lo que se ve y se cambia en TSF no depende de los permisos de cada persona.
En los logs de originabot queda ese usuario, no la persona.

El token se pide a Unergy Auth (`ORIGINABOT_AUTH_URL`, del mismo entorno que
`ORIGINABOT_URL`), se guarda por proceso hasta poco antes de vencer y, si
originabot lo rechaza igual, se pide otro una vez.

Lo usan la unión de PPAs (`apps/ppa/services/originabot_sync.py`), las cuentas
de cobro de arriendos (`apps/arriendos/services/cuentas_cobro_originabot.py`) y
la minigranja de cada planta (`apps/proyectos/services/originabot_minigranjas.py`).
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT = 20.0

#: Margen antes del vencimiento del token para pedir otro.
MARGEN_VENCIMIENTO_S = 60
#: Si el token no dice cuándo vence, cuánto se usa.
VIGENCIA_POR_DEFECTO_S = 240


class OriginabotError(Exception):
    """originabot no hizo lo pedido. El mensaje es apto para mostrarle a una persona."""


_candado = threading.Lock()
_token: str | None = None
_vence: float = 0.0


def _vencimiento(token: str) -> float:
    """El `exp` del JWT (sin verificar la firma: solo se usa para renovarlo a tiempo)."""
    try:
        carga = token.split(".")[1]
        datos = json.loads(base64.urlsafe_b64decode(carga + "=" * (-len(carga) % 4)))
        return float(datos["exp"]) - MARGEN_VENCIMIENTO_S
    except Exception:
        return time.time() + VIGENCIA_POR_DEFECTO_S


def _login() -> str:
    usuario, clave = settings.ORIGINABOT_USER, settings.ORIGINABOT_PASSWORD
    if not (usuario and clave):
        raise OriginabotError(
            "Operaciones no tiene configurado su usuario de originabot "
            "(ORIGINABOT_USER y ORIGINABOT_PASSWORD)."
        )
    url = f"{settings.ORIGINABOT_AUTH_URL.rstrip('/')}/auth/login/"
    # dj-rest-auth: un correo va como `email`; cualquier otra cosa, como `username`.
    cuerpo = {"email": usuario} if "@" in usuario else {"username": usuario}
    try:
        r = httpx.post(url, json={**cuerpo, "password": clave}, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        logger.error("Unergy Auth no respondió al login de originabot (%s): %s", url, exc)
        raise OriginabotError("No se pudo entrar a originabot: Unergy Auth no respondió.") from exc
    if r.status_code in (400, 401):
        logger.error("Unergy Auth rechazó el usuario de originabot en %s: %s", url, r.text[:300])
        raise OriginabotError(
            "originabot rechazó el usuario de Operaciones: revisa ORIGINABOT_USER y "
            "ORIGINABOT_PASSWORD (y que ORIGINABOT_AUTH_URL sea del mismo entorno)."
        )
    if r.status_code >= 400:
        raise OriginabotError(f"No se pudo entrar a originabot (Unergy Auth respondió {r.status_code}).")
    datos = r.json()
    token = datos.get("access") or datos.get("access_token")
    if not token:
        raise OriginabotError("Unergy Auth respondió sin token de acceso.")
    return token


def token_de_servicio(renovar: bool = False) -> str:
    """El token del usuario de Operaciones, del caché si sigue vigente."""
    global _token, _vence
    with _candado:
        if renovar or _token is None or time.time() >= _vence:
            _token = _login()
            _vence = _vencimiento(_token)
        return _token


class ClienteOriginabot:
    def _pedir(self, metodo: str, ruta: str, **kwargs) -> dict | list:
        url = f"{settings.ORIGINABOT_URL.rstrip('/')}{ruta}"
        r = self._enviar(metodo, url, token_de_servicio(), **kwargs)
        if r.status_code == 401:
            # Vencido o revocado antes de lo que decía: uno nuevo, una vez.
            r = self._enviar(metodo, url, token_de_servicio(renovar=True), **kwargs)
        if r.status_code in (401, 403):
            logger.error("originabot rechazó al usuario de Operaciones (%s %s): %s", metodo, url, r.status_code)
            raise OriginabotError(
                "originabot no le permite esto al usuario de Operaciones "
                f"({r.status_code}): revisa sus permisos en originabot."
            )
        if r.status_code == 404:
            raise OriginabotError(f"originabot no encontró {ruta}.")
        if r.status_code >= 400:
            logger.error("originabot respondió %s en %s %s: %s", r.status_code, metodo, url, r.text[:500])
            raise OriginabotError(f"originabot rechazó el cambio ({r.status_code}): {r.text[:300]}")
        return r.json()

    @staticmethod
    def _enviar(metodo: str, url: str, token: str, **kwargs) -> httpx.Response:
        try:
            return httpx.request(
                metodo, url, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT, **kwargs
            )
        except httpx.HTTPError as exc:
            logger.error("originabot no respondió (%s %s): %s", metodo, url, exc)
            raise OriginabotError("originabot no respondió.") from exc

    # ── PPAs (`investment.PPA`) ───────────────────────────────────────────────

    def ppa(self, ppa_id: int) -> dict:
        return self._pedir("GET", f"/api/investment/ppas/{ppa_id}/")

    def ppas(self) -> list[dict]:
        datos = self._pedir("GET", "/api/investment/ppas/", params={"page_size": 200})
        return datos.get("results", []) if isinstance(datos, dict) else datos

    def offtakers(self) -> list[dict]:
        """El catálogo de compradores. originabot no lo lista aparte: viene como
        `choices` del campo `off_taker` en el OPTIONS de la colección de PPAs."""
        datos = self._pedir("OPTIONS", "/api/investment/ppas/")
        campo = ((datos.get("actions") or {}).get("POST") or {}).get("off_taker") or {}
        return [
            {"id": c["id"], "nombre": c["name"]}
            for c in campo.get("choices") or []
            if isinstance(c, dict) and "id" in c and "name" in c
        ]

    def crear_ppa(self, nombre: str, off_taker_id: int, minifarms: list[int]) -> dict:
        return self._pedir(
            "POST", "/api/investment/ppas/",
            json={"name": nombre, "off_taker": off_taker_id, "minifarms": minifarms},
        )

    def fijar_minifarms(self, ppa_id: int, minifarms: list[int]) -> dict:
        """PATCH reemplaza la lista entera: quien llama ya la calculó con el delta."""
        return self._pedir("PATCH", f"/api/investment/ppas/{ppa_id}/", json={"minifarms": minifarms})

    # ── Minigranjas ───────────────────────────────────────────────────────────

    def buscar_minifarms(self, texto: str) -> list[dict]:
        """Minigranjas cuyo nombre o código contiene `texto` (`{id, name}`). La
        búsqueda de originabot indexa también el código CREG, así que encuentra una
        minigranja aunque la hayan renombrado («Minigranja 0030 - Chimá Oriente»)."""
        datos = self._pedir("GET", "/api/connection/minifarm-minimal/", params={"search": texto})
        return [
            m for m in (datos.get("results", []) if isinstance(datos, dict) else [])
            if isinstance(m.get("id"), int)
        ]

    def proyectos(self, page_size: int = 1) -> list[dict]:
        """La primera página de proyectos de minigranja (`/api/minifarm/projects/`)."""
        datos = self._pedir("GET", "/api/minifarm/projects/", params={"page_size": page_size})
        return datos.get("results", []) if isinstance(datos, dict) else datos

    def proyecto(self, proyecto_id: int) -> dict:
        """El detalle de un proyecto: el único que trae `grid_operator_data` con el catálogo."""
        return self._pedir("GET", f"/api/minifarm/projects/{proyecto_id}/")

    def minigranja(self, minifarm_id: int) -> dict:
        """El detalle de una minigranja: `project.id` y `project.terrain_id` salen de aquí."""
        return self._pedir("GET", f"/api/investment/minifarm/{minifarm_id}/")

    # ── Pagos de arriendo (`payment.Payment`) ─────────────────────────────────

    def pagos_de_terreno(self, terreno_id: int, anio: int) -> list[dict]:
        """Los pagos de arriendo de un terreno en un año, todas las páginas.

        Cada pago es de UN propietario y UNA fecha, y puede cubrir varios
        proyectos del terreno (`rent[].project`). `support_document.file` es la
        cuenta de cobro o factura que subió el propietario (URL firmada).
        """
        pagos: list[dict] = []
        pagina = 1
        while pagina <= 50:  # tope: un `next` roto no deja un bucle infinito
            datos = self._pedir(
                "GET", "/api/v2/origination/payment/payment/payment/",
                params={"terrain": terreno_id, "year": anio, "page": pagina, "page_size": 100},
            )
            pagos.extend(datos.get("results") or [])
            if not datos.get("next"):
                break
            pagina += 1
        return pagos

    @staticmethod
    def descargar(url: str) -> bytes:
        """Un archivo de originabot por su URL firmada (Backblaze/S3).

        SIN el token: la firma va en la URL, y el Bearer del usuario de
        Operaciones no tiene por qué salir hacia el bucket.
        """
        try:
            r = httpx.get(url, timeout=60.0, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise OriginabotError("No se pudo descargar el documento de TSF.") from exc
        if r.status_code >= 400:
            raise OriginabotError(
                f"TSF no entregó el documento ({r.status_code}); puede que su enlace haya vencido."
            )
        return r.content
