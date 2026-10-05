"""El ON/OFF de los reconectadores: SolarView con el token, detrás de tres candados.

Abrir o cerrar un reconectador energiza o apaga una planta REAL, y puede haber
gente trabajando en sitio. Por eso:

  - el interruptor `RECONECTADORES_COMANDOS_HABILITADOS` está apagado por
    defecto, y se revisa dentro del servicio, no solo en la vista;
  - solo `admin` y `operaciones` pueden mandar el comando, y con el usuario y
    la contraseña de SolarView de quien lo manda;
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
    monkeypatch.setattr(servicio, "_fila_interruptor", lambda: None)
    if valor is None:
        monkeypatch.delenv("RECONECTADORES_COMANDOS_HABILITADOS", raising=False)
    else:
        monkeypatch.setenv("RECONECTADORES_COMANDOS_HABILITADOS", valor)

    monkeypatch.setattr(servicio, "verificar_credenciales",
                        lambda *a: pytest.fail("no debía ir a sole.tech"))

    with pytest.raises(servicio.ComandosDeshabilitados):
        servicio.enviar_comando(17, "OFF", "ana", "x")

    assert capturar == []


def test_encendido_manda_el_comando_a_solarview_con_el_token(servicio, capturar, monkeypatch):
    import json

    monkeypatch.setenv("RECONECTADORES_COMANDOS_HABILITADOS", "true")
    monkeypatch.setattr(servicio, "verificar_credenciales", lambda u, c: None)

    respuesta = servicio.enviar_comando(17, "OFF", "ana", "x")

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

    monkeypatch.setattr(servicio, "verificar_credenciales",
                        lambda *a: pytest.fail("no debía ir a sole.tech"))

    with pytest.raises(ValueError):
        servicio.enviar_comando(17, "TOGGLE", "ana", "x")

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
    vista = _vista(accion)
    vista.request = request
    return RolePermission().has_permission(request, vista)


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


def test_el_comando_pide_usuario_y_contrasena_de_solarview():
    from api.v1.reconectadores.serializers import ComandoSerializer

    assert not ComandoSerializer(data={"accion": "OFF"}).is_valid()

    entrada = ComandoSerializer(data={"accion": "OFF", "username": "ana", "password": " c l "})
    assert entrada.is_valid(), entrada.errors
    # Los espacios son parte de la contraseña: no se recortan.
    assert entrada.validated_data == {"accion": "OFF", "username": "ana", "password": " c l "}
    # Y nunca vuelve en una respuesta.
    assert "password" not in entrada.data


# ── Las credenciales ────────────────────────────────────────────────────────


@pytest.fixture
def login(monkeypatch, servicio):
    """Reemplaza el httpx del servicio por un login de sole.tech con la respuesta dada."""
    peticiones: list[httpx.Request] = []
    original = httpx.Client
    estado = {"code": 200, "json": {"access": "jwt", "refresh": "r"}}

    def cliente(*args, **kwargs):
        def contestar(request):
            peticiones.append(request)
            return httpx.Response(estado["code"], json=estado["json"])

        return original(*args, transport=httpx.MockTransport(contestar), **kwargs)

    monkeypatch.setattr(servicio.httpx, "Client", cliente)
    return peticiones, estado


def test_credenciales_correctas_pasan_por_el_login_de_sole_tech(servicio, login):
    import json

    peticiones, _ = login

    servicio.verificar_credenciales("ana", "clave")

    [peticion] = peticiones
    assert str(peticion.url) == "https://auth.sole.tech/api/token/"
    assert json.loads(peticion.content) == {"username": "ana", "password": "clave"}


@pytest.mark.parametrize("code", [400, 401])
def test_credenciales_incorrectas_se_rechazan(servicio, login, code):
    _, estado = login
    estado.update(code=code, json={"detail": "No active account"})

    with pytest.raises(servicio.CredencialesInvalidas):
        servicio.verificar_credenciales("ana", "mala")


def test_un_login_que_no_devuelve_token_no_cuenta_como_valido(servicio, login):
    _, estado = login
    estado.update(code=500, json={})

    with pytest.raises(servicio.SolarViewNoResponde):
        servicio.verificar_credenciales("ana", "clave")


def _post_comando(monkeypatch, cuerpo):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.v1.reconectadores import views

    proyecto = SimpleNamespace(id=5, nombre_comercial="Planta", project_id_solarview="17")
    monkeypatch.setattr(views, "get_object_or_404", lambda *a, **k: proyecto)
    request = APIRequestFactory().post("/api/v1/reconectadores/5/comando", cuerpo, format="json")
    force_authenticate(request, user=SimpleNamespace(id=1, roles=["operaciones"], is_authenticated=True))
    return views.ReconectadorViewSet.as_view({"post": "comando"})(request, pk=5)


def test_con_el_interruptor_apagado_no_se_prueban_las_credenciales(monkeypatch, servicio):
    monkeypatch.delenv("RECONECTADORES_COMANDOS_HABILITADOS", raising=False)
    monkeypatch.setattr(servicio, "_fila_interruptor", lambda: None)
    monkeypatch.setattr(servicio, "verificar_credenciales",
                        lambda *a: pytest.fail("no debía ir a sole.tech"))

    respuesta = _post_comando(monkeypatch, {"accion": "OFF", "username": "ana", "password": "x"})

    assert respuesta.status_code == 503


def test_con_credenciales_incorrectas_no_sale_el_comando(monkeypatch, servicio):
    monkeypatch.setenv("RECONECTADORES_COMANDOS_HABILITADOS", "true")

    def rechazar(*a):
        raise servicio.CredencialesInvalidas("Usuario o contraseña de SolarView incorrectos.")

    monkeypatch.setattr(servicio, "verificar_credenciales", rechazar)
    monkeypatch.setattr(servicio, "cliente", lambda: pytest.fail("no debía salir"))

    respuesta = _post_comando(monkeypatch, {"accion": "OFF", "username": "ana", "password": "x"})

    assert respuesta.status_code == 400
    assert "incorrectos" in respuesta.data["detail"]


def test_con_credenciales_correctas_sale_el_comando(monkeypatch, servicio):
    monkeypatch.setenv("RECONECTADORES_COMANDOS_HABILITADOS", "true")
    enviados = []
    monkeypatch.setattr(servicio, "verificar_credenciales", lambda u, c: None)
    monkeypatch.setattr(servicio, "enviar_comando",
                        lambda sv, acc, u, c: enviados.append((sv, acc, u)) or httpx.Response(200, text="ok"))

    respuesta = _post_comando(monkeypatch, {"accion": "ON", "username": "ana", "password": "x"})

    assert respuesta.status_code == 200
    assert enviados == [(17, "ON", "ana")]


# ── El interruptor desde la plataforma ──────────────────────────────────────


def test_la_fila_encendida_habilita_los_comandos(monkeypatch, servicio):
    monkeypatch.delenv("RECONECTADORES_COMANDOS_HABILITADOS", raising=False)
    monkeypatch.setattr(servicio, "_fila_interruptor", lambda: SimpleNamespace(habilitado=True))

    assert servicio.comandos_habilitados() is True


@pytest.mark.parametrize("fila", [None, SimpleNamespace(habilitado=False)])
def test_sin_fila_o_con_la_fila_apagada_no_hay_comandos(monkeypatch, servicio, fila):
    monkeypatch.delenv("RECONECTADORES_COMANDOS_HABILITADOS", raising=False)
    monkeypatch.setattr(servicio, "_fila_interruptor", lambda: fila)

    assert servicio.comandos_habilitados() is False


def test_si_la_base_no_responde_el_interruptor_cuenta_como_apagado(monkeypatch, servicio):
    monkeypatch.delenv("RECONECTADORES_COMANDOS_HABILITADOS", raising=False)

    def caida():
        raise RuntimeError("sin base")

    monkeypatch.setattr(servicio, "_fila_interruptor", caida)

    assert servicio.comandos_habilitados() is False


def test_el_env_sigue_encendiendo_aunque_la_fila_diga_apagado(monkeypatch, servicio):
    monkeypatch.setenv("RECONECTADORES_COMANDOS_HABILITADOS", "true")
    monkeypatch.setattr(servicio, "_fila_interruptor", lambda: SimpleNamespace(habilitado=False))

    assert servicio.comandos_habilitados() is True


@pytest.mark.parametrize("roles, puede", [
    (["admin"], True),
    (["operaciones"], False),
    (["monitoreo"], False),
    (["solo_lectura"], False),
])
def test_solo_admin_cambia_el_interruptor(roles, puede):
    assert _puede(roles, "interruptor", "POST") is puede


def test_cualquiera_ve_el_estado_del_interruptor():
    assert _puede(["operaciones"], "interruptor", "GET")
    assert _puede(["solo_lectura"], "interruptor", "GET")
