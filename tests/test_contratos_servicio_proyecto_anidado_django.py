"""GET/PATCH /contratos-servicio (arbol Django) nunca anidaba el proyecto.

`ContratoSerializer` excluye el campo de modelo `proyecto` (Meta.exclude) y solo
expone `proyecto_id` (entero) y `nombre_proyecto` (string plano via
SerializerMethodField) -- pero el frontend (ServiciosUnificadoView.vue) decide
si mostrar el chip de planta o el boton "Sin proyecto" mirando `data.proyecto`
(un objeto anidado con `.id`, `.nombre_comercial`, `.tipo_proyecto`), que la API
nunca mandaba. Resultado: TODO contrato de servicio se veia "Sin proyecto" en
la pestana Servicios, incluso los que si tenian una planta asociada -- y
asociar una desde el dialogo tampoco lo reflejaba, porque la fila se reemplaza
con la respuesta del PATCH tal cual (ver guardarProyectoContrato en el
frontend), que tampoco traia el objeto anidado.

Mismo patron de bug que documenta tests/test_fallas_django.py: un campo que el
frontend necesita, silenciosamente ausente en el port a Django.
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


def _proyecto(**kw):
    from apps.proyectos.models import Proyecto

    kw.setdefault("nombre_comercial", "Proyecto")
    return Proyecto.objects.create(**kw)


def _contrato(**kw):
    from apps.contratos.models import ContratoServicio

    kw.setdefault("servicio_aplica", "representacion")
    return ContratoServicio.objects.create(**kw)


def test_get_detalle_anida_el_proyecto_asociado(datos):
    proyecto = _proyecto(nombre_comercial="Minigranja Test", tipo_proyecto="minigranja")
    contrato = _contrato(proyecto=proyecto)

    respuesta = _pedir(
        "get", f"/api/v1/contratos-servicio/{contrato.id}", datos,
        acciones={"get": "retrieve"}, pk=contrato.id,
    )

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["proyecto"] == {
        "id": proyecto.id,
        "nombre_comercial": "Minigranja Test",
        "tipo_proyecto": "minigranja",
    }


def test_get_detalle_sin_proyecto_asociado_da_proyecto_null(datos):
    contrato = _contrato(proyecto=None)

    respuesta = _pedir(
        "get", f"/api/v1/contratos-servicio/{contrato.id}", datos,
        acciones={"get": "retrieve"}, pk=contrato.id,
    )

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["proyecto"] is None


def test_patch_asociar_proyecto_devuelve_el_proyecto_anidado(datos):
    """El caso real reportado: el dialogo "Asociar a un proyecto" reemplaza la
    fila con la respuesta de este PATCH tal cual -- si no trae `proyecto`
    anidado, la fila se sigue viendo "Sin proyecto" pese al 200 y al
    `proyecto_id` correcto."""
    proyecto = _proyecto(nombre_comercial="Planta Asociada", tipo_proyecto="gd")
    contrato = _contrato(proyecto=None)

    respuesta = _pedir(
        "patch", f"/api/v1/contratos-servicio/{contrato.id}", datos,
        cuerpo={"proyecto_id": proyecto.id},
        acciones={"patch": "partial_update"}, pk=contrato.id,
    )

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["proyecto_id"] == proyecto.id
    assert respuesta.data["proyecto"] == {
        "id": proyecto.id,
        "nombre_comercial": "Planta Asociada",
        "tipo_proyecto": "gd",
    }


def test_list_tambien_anida_el_proyecto(datos):
    proyecto = _proyecto(nombre_comercial="Listado Test", tipo_proyecto="autoconsumo")
    _contrato(proyecto=proyecto)

    respuesta = _pedir(
        "get", "/api/v1/contratos-servicio", datos, acciones={"get": "list"},
    )

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data[0]["proyecto"]["nombre_comercial"] == "Listado Test"
