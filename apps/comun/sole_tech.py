"""La autenticación de sole.tech: un token fijo en la cabecera, en todos sus hosts.

`api.sole.tech` (SolarView), `data.sole.tech` (Solenium) y
`sunfactory.sole.tech` (Sun Factory) se autentican igual, según su
documentación: "La autenticación de todos los endpoints se debe realizar
enviando el siguiente header: `Authorization: Token <TOKEN>`". Es el mismo
token para los tres, el que ya estaba cargado como `SOLARVIEW_TOKEN`.

Hasta el 2026-09-29 `data.sole.tech` y `sunfactory.sole.tech` entraban con
usuario y contraseña (`POST auth.sole.tech/api/token/` → JWT `Bearer`), y ese
login ya no funciona: todo lo que dependía de él --alarmas de inversores,
inversores de monitoreo y de puesta en marcha, la potencia del dashboard, el
estado de los reconectadores, el pipeline de Sun Factory-- estaba sin datos.

La variable sigue llamándose `SOLARVIEW_TOKEN` para no tener que tocar el
`ENV_FILE` del servidor: el nombre es histórico, no dice que sea solo de
SolarView.

**El ON/OFF de los reconectadores NO pasa por acá.** Sigue pidiendo el usuario
y la contraseña de quien aprieta el botón (`reconectadores.token_de_usuario`);
qué hacer con él se decide aparte.
"""
from __future__ import annotations

from app.services.mgs.solenium_client import SoleniumClient
from apps.comun.config import settings


def token() -> str:
    return settings.SOLARVIEW_TOKEN.strip()


def configurado() -> bool:
    return bool(token())


def cabeceras() -> dict[str, str]:
    return {"Authorization": f"Token {token()}"}


class SoleniumConToken(SoleniumClient):
    """`SoleniumClient` con el token de sole.tech en vez del login.

    Hereda en vez de editar el original porque `app/` se lee y no se extiende:
    las rutas de `data.sole.tech` y el manejo de respuestas siguen viviendo en
    un solo lugar, el cliente de `app/`, y acá solo cambia cómo se entra.

    El cliente original llama a `_ensure_token()` antes de cada petición y
    no sigue si `_access_token` queda vacío; con el token fijo no hay nada que
    pedir ni renovar, así que basta con dejarlo puesto.
    """

    @property
    def enabled(self) -> bool:
        return configurado()

    def _ensure_token(self):
        self._access_token = token() or None

    def _headers(self) -> dict[str, str]:
        return cabeceras()
