"""`GET /asic?contrato_ppa_id=`: los registros GESCON de un PPA, por LLAVE.

El filtro existía por texto (`contrato_interno`) pero no por la llave foránea,
que es la fuente de verdad del vínculo PPA↔GESCON. Sin él, el detalle del
contrato no tenía cómo pedir sus registros reales, y por eso `ppa_contratos`
cargaba una copia a mano de seis columnas —`gescon_*` y `codigo_sic`— que ningún
servicio del backend lee y que no pueden representar una relación uno-a-muchos:
el contrato 2 de producción tiene 32 registros. Ver `docs/DIAGNOSTICO_PPA.md` §4.

Los dos filtros conviven a propósito: el de texto es el respaldo histórico para
los registros que nunca recibieron la FK.
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
    """Dos PPA. El primero con dos registros GESCON; el segundo con uno.

    Y un cuarto registro que empareja por TEXTO con el primero pero no tiene la
    llave: es la situación real de 100 de los 220 registros de producción.
    """
    from django.db import transaction

    from apps.mercado_xm.models import AsicSolicitud
    from apps.plataforma.models import Usuario
    from apps.ppa.models import PpaContrato

    atomica = transaction.atomic()
    atomica.__enter__()

    usuario = Usuario.objects.create(
        nombre="QA", email="qa@unergy.io", rol="admin", activo=True
    )
    uno = PpaContrato.objects.create(numero_codigo_contrato="UNERGY-001", tipo_contrato="compra")
    otro = PpaContrato.objects.create(numero_codigo_contrato="UNERGY-002", tipo_contrato="venta")

    AsicSolicitud.objects.create(
        contrato_ppa_id=uno.id, contrato_interno="UNERGY-001", codigo_sic_contrato="87137",
    )
    AsicSolicitud.objects.create(
        contrato_ppa_id=uno.id, contrato_interno="UNERGY-001", codigo_sic_contrato="87138",
    )
    AsicSolicitud.objects.create(
        contrato_ppa_id=otro.id, contrato_interno="UNERGY-002", codigo_sic_contrato="90060",
    )
    AsicSolicitud.objects.create(
        contrato_ppa_id=None, contrato_interno="UNERGY-001", codigo_sic_contrato="99999",
    )

    yield {"usuario": usuario, "uno": uno, "otro": otro}

    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _listar(datos, querystring=""):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.asic.views import AsicViewSet

    peticion = APIRequestFactory().get(f"/api/v1/asic{querystring}")
    force_authenticate(peticion, user=UsuarioAutenticado(datos["usuario"]))
    respuesta = AsicViewSet.as_view({"get": "list"})(peticion)
    respuesta.render()
    return respuesta


def _sics(respuesta):
    return {r["codigo_sic_contrato"] for r in respuesta.data}


def test_filtra_por_la_llave_del_contrato(datos):
    respuesta = _listar(datos, f"?contrato_ppa_id={datos['uno'].id}")

    assert respuesta.status_code == 200, respuesta.data
    assert _sics(respuesta) == {"87137", "87138"}


def test_no_trae_los_registros_de_otro_contrato(datos):
    respuesta = _listar(datos, f"?contrato_ppa_id={datos['otro'].id}")

    assert _sics(respuesta) == {"90060"}


def test_el_filtro_por_llave_no_arrastra_los_que_solo_coinciden_por_texto(datos):
    """El registro sin FK comparte `contrato_interno` con el primero. Por llave
    NO sale; por texto sí. Que los dos filtros den distinto es el dato: dice
    cuántos registros quedaron sin enlazar."""
    por_llave = _listar(datos, f"?contrato_ppa_id={datos['uno'].id}")
    por_texto = _listar(datos, "?contrato_interno=UNERGY-001")

    assert "99999" not in _sics(por_llave)
    assert "99999" in _sics(por_texto)


def test_sin_filtro_salen_todos(datos):
    respuesta = _listar(datos)

    assert _sics(respuesta) == {"87137", "87138", "90060", "99999"}


def test_un_contrato_sin_registros_devuelve_lista_vacia(datos):
    from apps.ppa.models import PpaContrato

    solo = PpaContrato.objects.create(numero_codigo_contrato="UNERGY-003")

    respuesta = _listar(datos, f"?contrato_ppa_id={solo.id}")

    assert respuesta.status_code == 200
    assert respuesta.data == []
