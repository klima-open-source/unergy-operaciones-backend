"""La planta dice si está en comunidad, derivándolo de sus PPA.

`proyectos.es_comunidad_energetica` se eliminó en `proyectos/0008`: era una
segunda verdad, porque la comunidad se negocia en el PPA. Pero la derivación
nunca se termino de hacer, y quedaron dos residuos en el frontend:

- la pantalla del proyecto mostraba `proyecto.es_comunidad_energetica`, que el
  backend ya no mandaba: la sección salia siempre en "—";
- el formulario de creación seguia con su toggle y mandaba las dos columnas en
  el payload, asi que quien marcaba una planta como comunidad creia haberlo
  hecho y no pasaba nada.

Esto cierra el lado del backend. La regla NO se reimplementa: se usa
`comunidades.plantas_en_comunidad`, la misma que decide si la planta recibe
representación y CGM, para que no vuelvan a existir dos definiciones.
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
def entorno():
    from django.db import transaction

    atomica = transaction.atomic()
    atomica.__enter__()
    yield
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _planta(nombre="Planta QA"):
    from apps.proyectos.models import Proyecto

    return Proyecto.objects.create(nombre_comercial=nombre)


def _ppa_de_comunidad(planta, **kw):
    from apps.ppa.models import PpaContrato, PpaContratoProyecto

    kw.setdefault("es_comunidad_energetica", True)
    kw.setdefault("tipo_contrato", "venta")
    contrato = PpaContrato.objects.create(**kw)
    PpaContratoProyecto.objects.create(contrato=contrato, proyecto=planta)
    return contrato


def _serializar(planta):
    from api.v1.proyectos.serializers import ProyectoSerializer

    return ProyectoSerializer(planta).data


def test_una_planta_sin_ppa_no_esta_en_comunidad(entorno):
    assert _serializar(_planta())["es_comunidad_energetica"] is False


def test_una_planta_con_ppa_de_comunidad_si_lo_esta(entorno):
    planta = _planta()
    _ppa_de_comunidad(planta)

    assert _serializar(planta)["es_comunidad_energetica"] is True


def test_un_ppa_normal_no_la_pone_en_comunidad(entorno):
    planta = _planta()
    _ppa_de_comunidad(planta, es_comunidad_energetica=False)

    assert _serializar(planta)["es_comunidad_energetica"] is False


def test_la_fecha_de_entrada_futura_todavia_no_cuenta(entorno):
    """La entrada a la comunidad no coincide con el inicio del PPA."""
    from datetime import timedelta

    from apps.plataforma.services.fechas import hoy_col

    planta = _planta()
    _ppa_de_comunidad(planta, fecha_entrada_comunidad=hoy_col() + timedelta(days=30))

    assert _serializar(planta)["es_comunidad_energetica"] is False


def test_un_ppa_borrado_no_cuenta(entorno):
    from django.utils import timezone

    planta = _planta()
    contrato = _ppa_de_comunidad(planta)
    contrato.deleted_at = timezone.now()
    contrato.save(update_fields=["deleted_at"])

    assert _serializar(planta)["es_comunidad_energetica"] is False


def test_el_listado_no_consulta_una_vez_por_planta(entorno):
    """El set se calcula UNA vez por serializacion: con una consulta por fila,
    un listado de 200 plantas haria 200 consultas."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from api.v1.proyectos.serializers import ProyectoSerializer

    plantas = [_planta(f"Planta {i}") for i in range(5)]
    _ppa_de_comunidad(plantas[0])

    serializer = ProyectoSerializer(plantas, many=True)
    with CaptureQueriesContext(connection) as consultas:
        datos = serializer.data

    del datos
    sobre_comunidad = [
        c for c in consultas.captured_queries
        if "ppa_contrato_proyectos" in c["sql"] and "es_comunidad" in c["sql"].lower()
    ]
    assert len(sobre_comunidad) <= 1, sobre_comunidad
