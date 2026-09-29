"""Todo sole.tech entra con `Authorization: Token <TOKEN>`, y nadie hace login.

Según la documentación de sole.tech: "La autenticación de todos los endpoints
se debe realizar enviando el siguiente header: `Authorization: Token <TOKEN>`".
Hasta el 2026-09-29 `data.sole.tech` y `sunfactory.sole.tech` entraban con
usuario y contraseña contra `auth.sole.tech`, y ese login ya no funciona: lo que
se fija acá es que ninguna de las puertas vivas lo vuelva a intentar.
"""
from types import SimpleNamespace

import httpx
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

TOKEN = "tok-de-prueba"


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


@pytest.fixture
def con_token(monkeypatch):
    monkeypatch.setenv("SOLARVIEW_TOKEN", TOKEN)


class _Registro:
    """Un transporte falso que anota cada petición y contesta 200."""

    def __init__(self, cuerpo=None):
        self.peticiones: list[httpx.Request] = []
        self._cuerpo = cuerpo if cuerpo is not None else {"results": []}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.peticiones.append(request)
        return httpx.Response(200, json=self._cuerpo)

    def fabrica(self):
        """Reemplazo de `httpx.Client` que conserva las cabeceras que le pasen."""
        registro = self
        # La clase original, tomada ANTES de que el test la reemplace: el
        # módulo reemplazado es el mismo `httpx` de acá.
        original = httpx.Client

        def cliente(*args, **kwargs):
            kwargs.pop("transport", None)
            return original(*args, transport=httpx.MockTransport(registro), **kwargs)

        return cliente


def _sin_login(peticiones):
    return not any("auth.sole.tech" in str(p.url) for p in peticiones)


# ── La cabecera ─────────────────────────────────────────────────────────────


def test_la_cabecera_es_token_y_no_bearer(con_token):
    from apps.comun import sole_tech

    assert sole_tech.cabeceras() == {"Authorization": f"Token {TOKEN}"}
    assert sole_tech.configurado()


def test_sin_token_no_esta_configurado(monkeypatch):
    from apps.comun import sole_tech

    monkeypatch.setenv("SOLARVIEW_TOKEN", "  ")

    assert not sole_tech.configurado()


# ── data.sole.tech con el cliente de app/ ───────────────────────────────────


def test_el_cliente_de_solenium_manda_el_token_sin_login(con_token):
    from apps.comun.sole_tech import SoleniumConToken

    registro = _Registro({"results": {"categories": []}})
    cliente = SoleniumConToken()
    cliente._http = httpx.Client(transport=httpx.MockTransport(registro))

    assert cliente.enabled
    cliente.get_availability()
    cliente.get_project_inverters(12)

    assert len(registro.peticiones) == 2
    assert all(p.headers["Authorization"] == f"Token {TOKEN}" for p in registro.peticiones)
    assert _sin_login(registro.peticiones)


def test_sin_token_el_cliente_de_solenium_no_sale(monkeypatch):
    from apps.comun.sole_tech import SoleniumConToken

    monkeypatch.setenv("SOLARVIEW_TOKEN", "")
    registro = _Registro()
    cliente = SoleniumConToken()
    cliente._http = httpx.Client(transport=httpx.MockTransport(registro))

    assert not cliente.enabled
    assert cliente.get_project_inverters(12) == []
    assert registro.peticiones == []


# ── Inversores de monitoreo (httpx directo) ─────────────────────────────────


def test_los_inversores_de_monitoreo_mandan_el_token_sin_login(con_token, monkeypatch):
    from apps.monitoreo.services import solenium_inversores

    registro = _Registro({"results": [{"id": 1}]})
    monkeypatch.setattr(solenium_inversores.httpx, "Client", registro.fabrica())

    inversores, error = solenium_inversores.inversores(
        SimpleNamespace(project_id_solenium="12", nombre_comercial="X", sub_project=None)
    )

    assert error is None
    assert inversores == [{"id": 1}]
    assert all(p.headers["Authorization"] == f"Token {TOKEN}" for p in registro.peticiones)
    assert _sin_login(registro.peticiones)


# ── Sun Factory ─────────────────────────────────────────────────────────────


def test_sun_factory_manda_el_token_sin_login(con_token, monkeypatch):
    from apps.proyectos.services import tsf_sync

    registro = _Registro({"results": [], "next": None})
    monkeypatch.setattr(tsf_sync.httpx, "Client", registro.fabrica())

    token = tsf_sync._sunfactory_token()
    tsf_sync._sunfactory_all_projects(token)
    tsf_sync._sunfactory_milestones_raw(token, 7)

    assert token == TOKEN
    assert len(registro.peticiones) == 2
    assert all(p.headers["Authorization"] == f"Token {TOKEN}" for p in registro.peticiones)
    assert _sin_login(registro.peticiones)


def test_sin_token_sun_factory_no_tiene_con_que_entrar(monkeypatch):
    from apps.proyectos.services import tsf_sync

    monkeypatch.setenv("SOLARVIEW_TOKEN", "")

    assert tsf_sync._sunfactory_token() is None
