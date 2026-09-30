"""El ON/OFF de los reconectadores: SolarView con el token, detrás de tres candados.

Abrir o cerrar un reconectador energiza o apaga una planta REAL, y puede haber
gente trabajando en sitio. Por eso:

  - el interruptor `RECONECTADORES_COMANDOS_HABILITADOS` está apagado por
    defecto, y se revisa dentro del servicio, no solo en la vista;
  - solo `admin` y `operaciones` pueden mandar el comando;
  - y NINGÚN test sale a la red: el transporte de httpx está bloqueado en todo
    el archivo. `recloser/set-status/` no se llama nunca durante el desarrollo.
"""
from types import SimpleNamespace

import httpx
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


@pytest.fixture(autouse=True)
def sin_red(monkeypatch):
    """Si algún test llegara a salir a la red de verdad, falla antes de enviar."""
    def prohibido(self, request):
        raise AssertionError(f"un test intentó salir a la red: {request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", prohibido)


class _SolarView:
    _base_url = "https://api.sole.tech"
    enabled = True

    def _headers(self):
        return {"Authorization": "Token tok-de-prueba"}


@pytest.fixture
def servicio(monkeypatch):
    from apps.monitoreo.services import reconectadores

    monkeypatch.setattr(reconectadores, "cliente", lambda: _SolarView())
    return reconectadores


@pytest.fixture
def capturar(monkeypatch, servicio):
    """Reemplaza el httpx del servicio por uno que anota la petición y contesta 200."""
    peticiones: list[httpx.Request] = []
    original = httpx.Client

    def cliente(*args, **kwargs):
        def anotar(request):
            peticiones.append(request)
            return httpx.Response(200, json={"success": True})

        return original(*args, transport=httpx.MockTransport(anotar), **kwargs)

    monkeypatch.setattr(servicio.httpx, "Client", cliente)
    return peticiones


# ── El interruptor ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("valor", [None, "", "false", "0", "si", "yes", "1"])
def test_con_el_interruptor_apagado_no_sale_ningun_comando(servicio, capturar, monkeypatch, valor):
    """Solo la palabra `true` lo enciende; ausente o cualquier otra cosa, apagado."""
    if valor is None:
        monkeypatch.delenv("RECONECTADORES_COMANDOS_HABILITADOS", raising=False)
    else:
        monkeypatch.setenv("RECONECTADORES_COMANDOS_HABILITADOS", valor)

    with pytest.raises(servicio.ComandosDeshabilitados):
        servicio.enviar_comando(17, "OFF")

    assert capturar == []


def test_encendido_manda_el_comando_a_solarview_con_el_token(servicio, capturar, monkeypatch):
    import json

    monkeypatch.setenv("RECONECTADORES_COMANDOS_HABILITADOS", "true")

    respuesta = servicio.enviar_comando(17, "OFF")

    assert respuesta.status_code == 200
    [peticion] = capturar
    assert peticion.method == "POST"
    # La forma que manda la plataforma de SolarView (capturada el 2026-09-30):
    # `/api/` delante, y el id en el CUERPO como `recloser`, no en la URL.
    assert str(peticion.url) == "https://api.sole.tech/api/solarview/config/recloser/set-status/"
    assert json.loads(peticion.content) == {"recloser": 17, "command": "OFF"}
    assert peticion.headers["Authorization"] == "Token tok-de-prueba"


def test_una_accion_que_no_es_on_ni_off_no_sale(servicio, capturar, monkeypatch):
    monkeypatch.setenv("RECONECTADORES_COMANDOS_HABILITADOS", "true")

    with pytest.raises(ValueError):
        servicio.enviar_comando(17, "TOGGLE")

    assert capturar == []


def test_el_interruptor_no_esta_encendido_en_ningun_ejemplo_de_env():
    """El `.env.example` no puede traerlo encendido: quien lo copie tal cual
    quedaría con comandos reales habilitados."""
    from pathlib import Path

    ejemplo = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")

    assert "RECONECTADORES_COMANDOS_HABILITADOS=true" not in ejemplo.lower().replace(" ", "")


# ── Quién puede ─────────────────────────────────────────────────────────────


def _vista(accion):
    from api.v1.reconectadores.views import ReconectadorViewSet

    vista = ReconectadorViewSet()
    vista.action = accion
    return vista


def _puede(roles, accion, metodo):
    from api.permissions import RolePermission

    request = SimpleNamespace(user=SimpleNamespace(roles=roles), method=metodo)
    return RolePermission().has_permission(request, _vista(accion))


@pytest.mark.parametrize("roles, puede", [
    (["admin"], True),
    (["operaciones"], True),
    (["comercial"], False),
    (["solo_lectura"], False),
    (["solo_lectura", "operaciones"], True),
])
def test_solo_admin_y_operaciones_mandan_el_comando(roles, puede):
    assert _puede(roles, "comando", "POST") is puede


def test_leer_el_estado_no_exige_rol():
    assert _puede(["comercial"], "estados", "GET")
    assert _puede(["solo_lectura"], "estados", "GET")


def test_el_comando_ya_no_pide_credenciales():
    from api.v1.reconectadores.serializers import ComandoSerializer

    campos = set(ComandoSerializer().fields)

    assert "username" not in campos
    assert "password" not in campos
    entrada = ComandoSerializer(data={"accion": "OFF"})
    assert entrada.is_valid(), entrada.errors
    assert entrada.validated_data == {"accion": "OFF"}
