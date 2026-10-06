"""El token de sole.tech: `Authorization: Token <TOKEN>`.

Lo acepta `api.sole.tech` (SolarView), y solo ese host. Medido el 2026-09-29
con `diagnosticar_sole_tech`:

  - `api.sole.tech`: 200 con `Token`, 401 con `Bearer`.
  - `data.sole.tech` (Solenium, la API vieja): 401 "Token Invalido" con los
    dos esquemas. No se revive cambiando la cabecera; lo que dependía de ella
    se pasa a endpoints de SolarView.
  - `sunfactory.sole.tech`: con `Token` no lo lee ("No se han proporcionado
    credenciales"); con `Bearer` espera el JWT del login de `auth.sole.tech`.

La variable es `SOLARVIEW_TOKEN`, la misma que lee `SolarViewClient`
(`apps/comun/integraciones/solarview_client.py`).
"""
from __future__ import annotations

from apps.comun.config import settings


def token() -> str:
    return settings.SOLARVIEW_TOKEN.strip()


def configurado() -> bool:
    return bool(token())


def cabeceras() -> dict[str, str]:
    return {"Authorization": f"Token {token()}"}
