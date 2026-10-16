"""originabot va SIEMPRE con el usuario de Operaciones, nunca con el de la sesión.

El cliente pide el token a Unergy Auth con ORIGINABOT_USER/ORIGINABOT_PASSWORD,
lo reutiliza mientras esté vigente y, si originabot lo rechaza igual, pide otro
una vez. Contra un Unergy Auth y un originabot falsos: se prueba la regla, no la red.
"""
import base64
import json
import time

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _jwt(exp: float) -> str:
    carga = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{carga}.f"


class _Resp:
    def __init__(self, status, datos=None):
        self.status_code, self._datos, self.text = status, datos or {}, json.dumps(datos or {})

    def json(self):
        return self._datos


@pytest.fixture
def red(monkeypatch):
    from django.conf import settings

    from apps.comun.integraciones import originabot

    for nombre, valor in (
        ("ORIGINABOT_USER", "operaciones"), ("ORIGINABOT_PASSWORD", "secreto"),
        ("ORIGINABOT_AUTH_URL", "https://auth.test"), ("ORIGINABOT_URL", "https://ob.test"),
    ):
        monkeypatch.setattr(settings, nombre, valor)
    monkeypatch.setattr(originabot, "_token", None)
    monkeypatch.setattr(originabot, "_vence", 0.0)

    estado = {"logins": [], "tokens": [], "rechazar": set(), "exp": time.time() + 3600}

    def post(url, json=None, **_):
        estado["logins"].append((url, json))
        return _Resp(200, {"access": _jwt(estado["exp"]) + str(len(estado["logins"]))})

    def request(metodo, url, headers=None, **_):
        token = headers["Authorization"].removeprefix("Bearer ")
        estado["tokens"].append(token)
        return _Resp(401) if token in estado["rechazar"] else _Resp(200, {"ok": True})

    monkeypatch.setattr(originabot.httpx, "post", post)
    monkeypatch.setattr(originabot.httpx, "request", request)
    return estado


def test_se_loguea_una_vez_y_reutiliza_el_token(red):
    from apps.comun.integraciones.originabot import ClienteOriginabot

    ClienteOriginabot().minigranja(1)
    ClienteOriginabot().minigranja(2)

    assert len(red["logins"]) == 1
    url, cuerpo = red["logins"][0]
    assert url == "https://auth.test/auth/login/"
    assert cuerpo == {"username": "operaciones", "password": "secreto"}
    assert red["tokens"][0] == red["tokens"][1]


def test_un_token_vencido_se_renueva(red):
    from apps.comun.integraciones.originabot import ClienteOriginabot

    red["exp"] = time.time() + 30  # dentro del margen: ya cuenta como vencido
    ClienteOriginabot().minigranja(1)
    ClienteOriginabot().minigranja(2)

    assert len(red["logins"]) == 2


def test_si_originabot_lo_rechaza_pide_otro_una_vez(red):
    from apps.comun.integraciones.originabot import ClienteOriginabot, token_de_servicio

    red["rechazar"].add(token_de_servicio())
    assert ClienteOriginabot().minigranja(1) == {"ok": True}
    assert len(red["logins"]) == 2


def test_sin_credenciales_lo_dice(monkeypatch):
    from django.conf import settings

    from apps.comun.integraciones import originabot

    monkeypatch.setattr(settings, "ORIGINABOT_USER", "")
    monkeypatch.setattr(originabot, "_token", None)
    with pytest.raises(originabot.OriginabotError, match="ORIGINABOT_USER"):
        originabot.ClienteOriginabot().minigranja(1)
