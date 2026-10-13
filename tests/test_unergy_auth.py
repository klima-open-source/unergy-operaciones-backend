"""Tokens de Unergy Auth (authentication-system) conviviendo con los propios.

`api.authentication.UnergyAuthAuthentication` valida por gRPC los `Bearer` que
no son nuestros y resuelve el usuario **por email contra `usuarios`**, con su rol
local. Lo que se vigila:

- con `GRPC_HOST` vacío (el default) nada cambia: un token ajeno es 401 y no se
  llama a nadie;
- un JWT propio sigue entrando sin pasar por gRPC;
- token válido de un email que no está en `usuarios` → 403, no 401: mandar al
  login a alguien que ya inició sesión en Unergy sería un bucle;
- salvo que su grupo dé alta automática (`operation_staff`): se le crea la
  cuenta con rol `operaciones`, sin contraseña local, y entra;
- una cuenta inactiva no se reactiva por tener el grupo;
- servicio caído → 503, no 401: no es culpa de la sesión;
- el cliente gRPC generado habla de verdad con un servidor (protobuf 7).
"""
from concurrent import futures

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

ME = "/api/v1/auth/me"


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)

    from django.conf import settings

    originales = settings.DATABASES
    settings.DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    }
    django.setup()

    from django.apps import apps as django_apps
    from django.db import connections
    from django.test.utils import setup_test_environment

    connections.close_all()
    connections.__dict__.pop("settings", None)
    connections.__init__()

    settings.MIGRATION_MODULES = {a.label: None for a in django_apps.get_app_configs()}
    setup_test_environment()
    connections["default"].creation.create_test_db(verbosity=0)
    assert connections["default"].vendor == "sqlite", "no se aisló de la base real"
    yield

    from django.test.utils import teardown_test_environment

    connections.close_all()
    teardown_test_environment()
    settings.DATABASES = originales
    connections.__dict__.pop("settings", None)
    connections.__init__()


@pytest.fixture(scope="module")
def usuario():
    from apps.plataforma.models import Usuario

    return Usuario.objects.create(
        email="ana@unergy.io", nombre="Ana", rol="monitoreo", activo=True,
    )


@pytest.fixture
def cliente():
    from django.test import Client

    return Client()


@pytest.fixture
def con_unergy_auth(settings_override):
    return settings_override(GRPC_HOST="auth.test", GRPC_PORT="50051", UNERGY_AUTH_HTTP_URL="")


@pytest.fixture
def settings_override():
    from django.test import override_settings

    activos = []

    def aplicar(**valores):
        o = override_settings(**valores)
        o.enable()
        activos.append(o)

    yield aplicar
    for o in reversed(activos):
        o.disable()


@pytest.fixture
def grpc_falso(monkeypatch):
    """Reemplaza la llamada de red; `respuesta` es una IdentidadUnergy o una excepción."""
    from apps.comun.integraciones.unergy_auth import cliente as unergy_auth

    estado = {"respuesta": None, "llamadas": []}

    def validar(token):
        estado["llamadas"].append(token)
        if isinstance(estado["respuesta"], Exception):
            raise estado["respuesta"]
        return estado["respuesta"]

    monkeypatch.setattr(unergy_auth, "validar_token", validar)
    return estado


def _identidad(email, grupos=("ops",)):
    from apps.comun.integraciones.unergy_auth.cliente import IdentidadUnergy

    return IdentidadUnergy(
        user_id=7, email=email, username="ana", nombre="Ana Pérez", grupos=grupos,
    )


def test_apagada_token_ajeno_es_401_sin_llamar_a_grpc(cliente, grpc_falso, settings_override):
    settings_override(GRPC_HOST="", UNERGY_AUTH_HTTP_URL="")
    r = cliente.get(ME, HTTP_AUTHORIZATION="Bearer token-de-unergy")
    assert r.status_code == 401
    assert grpc_falso["llamadas"] == []


def test_jwt_propio_sigue_entrando_sin_grpc(cliente, usuario, grpc_falso, con_unergy_auth):
    from apps.plataforma.services import cuentas

    r = cliente.get(ME, HTTP_AUTHORIZATION=f"Bearer {cuentas.token_de(usuario, movil=False)}")
    assert r.status_code == 200
    assert r.json()["email"] == "ana@unergy.io"
    assert grpc_falso["llamadas"] == []


def test_token_unergy_resuelve_usuario_local_por_email(cliente, usuario, grpc_falso, con_unergy_auth):
    # Mayúsculas a propósito: el email de authentication-system no tiene por qué
    # venir con la misma capitalización que en `usuarios`.
    grpc_falso["respuesta"] = _identidad("Ana@Unergy.io")
    r = cliente.get(ME, HTTP_AUTHORIZATION="Bearer token-de-unergy")
    assert r.status_code == 200
    assert r.json()["id"] == usuario.id
    assert r.json()["rol"] == "monitoreo"
    assert grpc_falso["llamadas"] == ["token-de-unergy"]


def test_email_sin_cuenta_en_operaciones_es_403(cliente, usuario, grpc_falso, con_unergy_auth):
    grpc_falso["respuesta"] = _identidad("nadie@unergy.io")
    r = cliente.get(ME, HTTP_AUTHORIZATION="Bearer token-de-unergy")
    assert r.status_code == 403


def test_operation_staff_nuevo_se_da_de_alta_y_entra(cliente, grpc_falso, con_unergy_auth):
    from apps.plataforma.models import Usuario

    grpc_falso["respuesta"] = _identidad("Nuevo@Unergy.io", grupos=("otro", "operation_staff"))
    r = cliente.get(ME, HTTP_AUTHORIZATION="Bearer token-de-unergy")

    assert r.status_code == 200
    assert r.json()["email"] == "nuevo@unergy.io"
    assert r.json()["rol"] == "operaciones"
    nuevo = Usuario.objects.get(email="nuevo@unergy.io")
    assert nuevo.nombre == "Ana Pérez"
    assert nuevo.activo
    # Entra solo por Unergy Auth: el login propio no tiene contraseña que probar.
    assert not nuevo.has_usable_password()

    # La segunda petición encuentra la cuenta: no duplica ni falla por el UNIQUE.
    r = cliente.get(ME, HTTP_AUTHORIZATION="Bearer token-de-unergy")
    assert r.status_code == 200
    assert Usuario.objects.filter(email__iexact="nuevo@unergy.io").count() == 1


def test_grupo_sin_alta_automatica_no_crea_cuenta(cliente, grpc_falso, con_unergy_auth):
    from apps.plataforma.models import Usuario

    grpc_falso["respuesta"] = _identidad("ajeno@unergy.io", grupos=("finance",))
    r = cliente.get(ME, HTTP_AUTHORIZATION="Bearer token-de-unergy")

    assert r.status_code == 403
    assert not Usuario.objects.filter(email__iexact="ajeno@unergy.io").exists()


def test_operation_staff_no_pisa_el_rol_de_una_cuenta_existente(
    cliente, usuario, grpc_falso, con_unergy_auth,
):
    grpc_falso["respuesta"] = _identidad("ana@unergy.io", grupos=("operation_staff",))
    r = cliente.get(ME, HTTP_AUTHORIZATION="Bearer token-de-unergy")

    assert r.status_code == 200
    assert r.json()["rol"] == "monitoreo"


def test_operation_staff_no_reactiva_una_cuenta_dada_de_baja(cliente, grpc_falso, con_unergy_auth):
    from apps.plataforma.models import Usuario

    Usuario.objects.create(email="retirado@unergy.io", nombre="R", rol="operaciones", activo=False)
    grpc_falso["respuesta"] = _identidad("retirado@unergy.io", grupos=("operation_staff",))
    r = cliente.get(ME, HTTP_AUTHORIZATION="Bearer token-de-unergy")

    assert r.status_code == 401
    assert not Usuario.objects.get(email="retirado@unergy.io").activo


def test_usuario_inactivo_es_401(cliente, grpc_falso, con_unergy_auth):
    from apps.plataforma.models import Usuario

    Usuario.objects.create(email="baja@unergy.io", nombre="Baja", rol="operaciones", activo=False)
    grpc_falso["respuesta"] = _identidad("baja@unergy.io")
    r = cliente.get(ME, HTTP_AUTHORIZATION="Bearer token-de-unergy")
    assert r.status_code == 401


def test_token_rechazado_es_401(cliente, grpc_falso, con_unergy_auth):
    from apps.comun.integraciones.unergy_auth.cliente import TokenInvalido

    grpc_falso["respuesta"] = TokenInvalido("vencido")
    r = cliente.get(ME, HTTP_AUTHORIZATION="Bearer token-de-unergy")
    assert r.status_code == 401


def test_servicio_caido_es_503(cliente, grpc_falso, con_unergy_auth):
    from apps.comun.integraciones.unergy_auth.cliente import ServicioNoDisponible

    grpc_falso["respuesta"] = ServicioNoDisponible()
    r = cliente.get(ME, HTTP_AUTHORIZATION="Bearer token-de-unergy")
    assert r.status_code == 503


@pytest.fixture
def servidor_grpc():
    """Un AuthService de verdad en un puerto local, para probar el código generado."""
    import grpc

    from apps.comun.integraciones.unergy_auth import auth_pb2, auth_pb2_grpc

    class Servicio(auth_pb2_grpc.AuthServiceServicer):
        def ValidateToken(self, request, context):
            if request.token != "bueno":
                return auth_pb2.UserResponse(
                    is_valid=False, error=auth_pb2.ErrorResponse(code=401, message="vencido"),
                )
            return auth_pb2.UserResponse(
                is_valid=True, user_id=7, username="ana", email="ana@unergy.io", groups=["ops"],
                first_name="Ana", last_name="Pérez",
            )

    servidor = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    auth_pb2_grpc.add_AuthServiceServicer_to_server(Servicio(), servidor)
    puerto = servidor.add_insecure_port("127.0.0.1:0")
    servidor.start()
    yield puerto
    servidor.stop(None)


def test_cliente_grpc_contra_servidor_real(servidor_grpc, settings_override):
    from apps.comun.integraciones.unergy_auth import cliente as unergy_auth

    settings_override(GRPC_HOST="127.0.0.1", GRPC_PORT=str(servidor_grpc), GRPC_TIMEOUT=5)

    identidad = unergy_auth.validar_token("bueno")
    assert identidad.email == "ana@unergy.io"
    assert identidad.grupos == ("ops",)
    assert identidad.nombre == "Ana Pérez"

    with pytest.raises(unergy_auth.TokenInvalido, match="vencido"):
        unergy_auth.validar_token("malo")


def test_cliente_grpc_sin_servidor_es_no_disponible(settings_override):
    from apps.comun.integraciones.unergy_auth import cliente as unergy_auth

    # Puerto 9 (discard): nadie escucha. Timeout corto para no frenar la suite.
    settings_override(GRPC_HOST="127.0.0.1", GRPC_PORT="9", GRPC_TIMEOUT=0.5)
    with pytest.raises(unergy_auth.ServicioNoDisponible):
        unergy_auth.validar_token("cualquiera")


# ── Validación por HTTP (solo desarrollo, sin acceso a la red del gRPC) ──────

class _RespuestaHttp:
    def __init__(self, status_code, datos=None):
        self.status_code = status_code
        self._datos = datos or {}

    def json(self):
        return self._datos


@pytest.fixture
def http_falso(monkeypatch, settings_override):
    from apps.comun.integraciones.unergy_auth import cliente as unergy_auth

    settings_override(GRPC_HOST="", UNERGY_AUTH_HTTP_URL="https://devauth.test/", GRPC_TIMEOUT=5)
    estado = {"respuesta": None, "llamadas": []}

    def get(url, headers, timeout):
        estado["llamadas"].append((url, headers["Authorization"]))
        if isinstance(estado["respuesta"], Exception):
            raise estado["respuesta"]
        return estado["respuesta"]

    monkeypatch.setattr(unergy_auth.httpx, "get", get)
    return estado


def test_http_lee_usuario_de_dj_rest_auth(http_falso):
    from apps.comun.integraciones.unergy_auth import cliente as unergy_auth

    http_falso["respuesta"] = _RespuestaHttp(200, {
        "pk": 9, "username": "ana", "email": "ana@unergy.io",
        "first_name": "Ana", "last_name": "Pérez",
        "groups": [{"id": 1, "name": "operation_staff"}, "otro"],
    })
    identidad = unergy_auth.validar_token("tok")

    assert http_falso["llamadas"] == [("https://devauth.test/auth/user/", "Bearer tok")]
    assert identidad.email == "ana@unergy.io"
    assert identidad.nombre == "Ana Pérez"
    assert identidad.grupos == ("operation_staff", "otro")


def test_http_token_rechazado(http_falso):
    from apps.comun.integraciones.unergy_auth import cliente as unergy_auth

    http_falso["respuesta"] = _RespuestaHttp(401)
    with pytest.raises(unergy_auth.TokenInvalido):
        unergy_auth.validar_token("tok")


def test_http_caido_es_no_disponible(http_falso):
    import httpx

    from apps.comun.integraciones.unergy_auth import cliente as unergy_auth

    http_falso["respuesta"] = httpx.ConnectError("sin red")
    with pytest.raises(unergy_auth.ServicioNoDisponible):
        unergy_auth.validar_token("tok")
    http_falso["respuesta"] = _RespuestaHttp(502)
    with pytest.raises(unergy_auth.ServicioNoDisponible):
        unergy_auth.validar_token("tok")


def test_http_no_se_acepta_fuera_de_development():
    """Validar por HTTP se aparta del lineamiento (gRPC): fuera de development
    el arranque tiene que fallar, no aceptarlo en silencio."""
    import os
    import subprocess
    import sys

    entorno = {
        **os.environ, "ENVIRONMENT": "production", "SECRET_KEY": "x" * 40,
        "UNERGY_AUTH_HTTP_URL": "https://devauth.test", "DJANGO_SETTINGS_MODULE": "config.settings",
    }
    r = subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        env=entorno, capture_output=True, text=True,
    )
    assert r.returncode != 0
    assert "UNERGY_AUTH_HTTP_URL es solo para desarrollo" in r.stderr
