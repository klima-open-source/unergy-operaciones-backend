"""POST/PATCH /contratos-servicio descartaba en silencio el cliente de cada parte.

Mismo bug que `proyecto_id` (ver test_contratos_servicio_proyecto_anidado_django),
repetido en las tres partes: el FK se llama `contratante` en el modelo, así que
el campo que genera ModelSerializer también, y la clave `contratante_id` que
manda el frontend no la reconocía nadie. DRF ignora en silencio una clave que no
conoce: la API respondía 200 y el vínculo NO quedaba guardado.

Por eso la auditoría del 2026-08-27 encontró 0 de 162 contratos con el vínculo
puesto aunque el wizard ya tenía autocompletado, y por eso el reparto de costos
por inversionista tiene que emparejar comparando nombres.

`inversionista_id` es el que más duele: es el que decide qué tarifa de
representación se le cobra a cada inversionista de una minigranja.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


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


@pytest.fixture
def datos():
    from django.db import transaction

    from apps.plataforma.models import Usuario

    atomica = transaction.atomic()
    atomica.__enter__()
    usuario = Usuario.objects.create(nombre="QA", email="qa@unergy.io", rol="admin", activo=True)
    yield {"usuario": usuario}
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _pedir(metodo, url, datos_usuario, cuerpo=None, **kwargs):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.contratos_servicio.views import ContratoServicioViewSet

    factory = APIRequestFactory()
    peticion = getattr(factory, metodo)(url, cuerpo, format="json") if cuerpo is not None \
        else getattr(factory, metodo)(url)
    force_authenticate(peticion, user=UsuarioAutenticado(datos_usuario["usuario"]))
    vista = ContratoServicioViewSet.as_view(kwargs.pop("acciones"))
    respuesta = vista(peticion, **kwargs)
    respuesta.render()
    return respuesta


def _cliente(nombre, nit=None):
    from apps.clientes.models import Cliente

    return Cliente.objects.create(razon_social_nombre=nombre, nit_cedula=nit)


def _contrato(**kw):
    from apps.contratos.models import ContratoServicio

    kw.setdefault("servicio_aplica", "representacion")
    return ContratoServicio.objects.create(**kw)


def test_crear_guarda_las_tres_partes_vinculadas(datos):
    from apps.contratos.models import ContratoServicio

    contratante = _cliente("Quantum Energy Ingenieria S.A.S.", "900111222-3")
    prestador = _cliente("Unergy S.A.S.", "901333444-5")
    inversionista = _cliente("Fondo Solar Uno", "902555666-7")

    respuesta = _pedir(
        "post", "/api/v1/contratos-servicio", datos,
        cuerpo={
            "servicio_aplica": "representacion",
            "contratante_id": contratante.id,
            "prestador_id": prestador.id,
            "inversionista_id": inversionista.id,
        },
        acciones={"post": "create"},
    )

    assert respuesta.status_code == 201, respuesta.data
    guardado = ContratoServicio.objects.get(pk=respuesta.data["id"])
    assert guardado.contratante_id == contratante.id
    assert guardado.prestador_id == prestador.id
    assert guardado.inversionista_id == inversionista.id


def test_patch_vincula_el_inversionista(datos):
    """El caso que rompe el reparto de costos: sin este id, cada tarifa de
    representación se le aplica a todos los inversionistas por igual."""
    from apps.contratos.models import ContratoServicio

    inversionista = _cliente("Fondo Solar Dos", "903777888-9")
    contrato = _contrato(inversionista_nombre="Fondo Solar Dos")
    assert contrato.inversionista_id is None

    respuesta = _pedir(
        "patch", f"/api/v1/contratos-servicio/{contrato.id}", datos,
        cuerpo={"inversionista_id": inversionista.id},
        acciones={"patch": "partial_update"}, pk=contrato.id,
    )

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["inversionista_id"] == inversionista.id
    assert ContratoServicio.objects.get(pk=contrato.id).inversionista_id == inversionista.id


def test_sincronizar_copia_el_nombre_del_inversionista_vinculado(datos):
    """Vinculado el cliente, el nombre guardado es el suyo: deja de haber dos
    grafías del mismo inversionista según quién escribió el contrato."""
    from apps.contratos.models import ContratoServicio

    inversionista = _cliente("Fondo Solar Tres S.A.S.", "904999000-1")
    contrato = _contrato(inversionista_nombre="fondo solar tres")

    _pedir(
        "patch", f"/api/v1/contratos-servicio/{contrato.id}", datos,
        cuerpo={"inversionista_id": inversionista.id},
        acciones={"patch": "partial_update"}, pk=contrato.id,
    )

    guardado = ContratoServicio.objects.get(pk=contrato.id)
    assert guardado.inversionista_nombre == "Fondo Solar Tres S.A.S."


# ── La API exige las partes al CREAR ─────────────────────────────────────────
# La pantalla ya las pedía, pero una llamada directa, el CRM o una carga masiva
# seguían pudiendo crear partes sueltas. Solo al crear: al editar hay 160
# contratos sin vínculo y bloquear el guardado dejaría a cualquiera que corrija
# una fecha atrapado resolviendo datos maestros que no son suyos.

def test_crear_sin_contratante_lo_rechaza(datos):
    respuesta = _pedir(
        "post", "/api/v1/contratos-servicio", datos,
        cuerpo={"servicio_aplica": "mantenimiento",
                "prestador_id": _cliente("Unergy S.A.S.").id},
        acciones={"post": "create"},
    )

    assert respuesta.status_code == 400, respuesta.data
    assert "contratante_id" in respuesta.data


def test_crear_una_representacion_sin_inversionista_lo_rechaza(datos):
    """Solo en representación/CGM: es donde la tarifa varía por inversionista."""
    respuesta = _pedir(
        "post", "/api/v1/contratos-servicio", datos,
        cuerpo={
            "servicio_aplica": "representacion",
            "contratante_id": _cliente("Quantum").id,
            "prestador_id": _cliente("Unergy S.A.S.").id,
        },
        acciones={"post": "create"},
    )

    assert respuesta.status_code == 400, respuesta.data
    assert "inversionista_id" in respuesta.data


def test_un_mantenimiento_no_necesita_inversionista(datos):
    respuesta = _pedir(
        "post", "/api/v1/contratos-servicio", datos,
        cuerpo={
            "servicio_aplica": "mantenimiento",
            "contratante_id": _cliente("Quantum").id,
            "prestador_id": _cliente("Unergy S.A.S.").id,
        },
        acciones={"post": "create"},
    )

    assert respuesta.status_code == 201, respuesta.data


def test_editar_un_contrato_viejo_sin_partes_sigue_siendo_posible(datos):
    """Los 160 contratos sin vínculo tienen que poder corregirse mientras el
    backfill no corra: exigirlo acá dejaría a la gente atrapada."""
    contrato = _contrato(servicio_aplica="mantenimiento")

    respuesta = _pedir(
        "patch", f"/api/v1/contratos-servicio/{contrato.id}", datos,
        cuerpo={"numero_contrato": "UNERGY-OM-004"},
        acciones={"patch": "partial_update"}, pk=contrato.id,
    )

    assert respuesta.status_code == 200, respuesta.data


def test_desvincular_una_parte_sigue_siendo_posible(datos):
    """`null` tiene que poder borrar el vínculo: un contrato mal vinculado se
    corrige, no se queda atado al cliente equivocado."""
    from apps.contratos.models import ContratoServicio

    contratante = _cliente("Cliente Equivocado")
    contrato = _contrato(contratante=contratante)

    respuesta = _pedir(
        "patch", f"/api/v1/contratos-servicio/{contrato.id}", datos,
        cuerpo={"contratante_id": None},
        acciones={"patch": "partial_update"}, pk=contrato.id,
    )

    assert respuesta.status_code == 200, respuesta.data
    assert ContratoServicio.objects.get(pk=contrato.id).contratante_id is None
