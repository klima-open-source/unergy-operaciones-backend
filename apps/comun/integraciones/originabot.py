"""Cliente HTTP de originabot (TSF), con el token de Unergy Auth de quien pide.

El backend no tiene credenciales propias para originabot: cada llamada va con el
Bearer de la persona que hizo el cambio en Operaciones (`request.auth`, ver
`api.authentication.UnergyAuthAuthentication`), y originabot decide con SUS
permisos. Un token del login propio de Operaciones no le sirve a originabot.

Lo usan la unión de PPAs (`apps/ppa/services/originabot_sync.py`) y las cuentas
de cobro de arriendos (`apps/arriendos/services/cuentas_cobro_originabot.py`).
"""
from __future__ import annotations

import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT = 20.0


class OriginabotError(Exception):
    """originabot no hizo lo pedido. El mensaje es apto para mostrarle a una persona."""


class ClienteOriginabot:
    def __init__(self, token: str):
        self._base = settings.ORIGINABOT_URL.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    def _pedir(self, metodo: str, ruta: str, **kwargs) -> dict | list:
        url = f"{self._base}{ruta}"
        try:
            r = httpx.request(metodo, url, headers=self._headers, timeout=TIMEOUT, **kwargs)
        except httpx.HTTPError as exc:
            logger.error("originabot no respondió (%s %s): %s", metodo, url, exc)
            raise OriginabotError("originabot no respondió.") from exc
        if r.status_code in (401, 403):
            raise OriginabotError(
                "originabot rechazó tu sesión: entra con Unergy Auth (del mismo entorno) "
                "y con permiso sobre PPAs en originabot."
            )
        if r.status_code == 404:
            raise OriginabotError(f"originabot no encontró {ruta}.")
        if r.status_code >= 400:
            logger.error("originabot respondió %s en %s %s: %s", r.status_code, metodo, url, r.text[:500])
            raise OriginabotError(f"originabot rechazó el cambio ({r.status_code}): {r.text[:300]}")
        return r.json()

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

        SIN el token: la firma va en la URL, y el Bearer de Unergy Auth no tiene
        por qué salir hacia el bucket.
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
