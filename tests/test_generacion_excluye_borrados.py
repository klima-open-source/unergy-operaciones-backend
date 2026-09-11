"""Una planta borrada no aparece en las vistas de generación.

`Proyecto` guarda el borrado en `deleted_at` y **no tiene un manager que lo
filtre**: cada consulta tiene que excluirlo a mano. Es de las cosas que no
fallan cuando se olvidan -- la consulta corre igual, devuelve una fila de más, y
nadie se entera hasta que alguien pregunta por qué una planta dada de baja sigue
en un desplegable.

Estaba olvidado en los tres sitios que alimentan Generación Solar, que es lo que
se fija acá:

  · `proyectos_en_operacion()` -- el selector del Histórico y el resumen de
    flota. Una planta borrada se podía seleccionar, y al consultarla se le pedía
    la generación a la API de Unergy como si nada.
  · `_proyectos_en_operacion()` -- la generación de hoy y el resumen del día.
  · `monitoreo_flota()` -- las tarjetas de Tiempo Real, donde además contaminaba
    los contadores del encabezado (`fleet.total`, la capacidad sumada).

Las pruebas son sobre el filtro, no sobre los proveedores externos: se mira el
queryset, que es donde estaba el defecto.
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
def base_limpia():
    from django.db import transaction

    atomica = transaction.atomic()
    atomica.__enter__()
    yield
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _proyecto(nombre, **kw):
    from apps.proyectos.models import Proyecto

    campos = {
        "nombre_comercial": nombre,
        "estado": "en_operacion",
        "sub_project": nombre.lower().replace(" ", "-"),
        "tipo_proyecto": "minigranja",
        "srv_operacion": True,
    }
    campos.update(kw)
    return Proyecto.objects.create(**campos)


def _borrado(nombre, **kw):
    from django.utils import timezone

    return _proyecto(nombre, deleted_at=timezone.now(), **kw)


# ── El selector del Histórico ───────────────────────────────────────────────


def test_el_selector_del_historico_no_ofrece_una_planta_borrada(base_limpia):
    from api.v1.monitoreo.queryset import proyectos_en_operacion

    _proyecto("Planta Viva")
    _borrado("Planta Dada De Baja")

    nombres = [p.nombre_comercial for p in proyectos_en_operacion()]

    assert nombres == ["Planta Viva"]


def test_el_selector_sigue_pidiendo_sub_project(base_limpia):
    """El otro filtro no se perdió: sin `sub_project` la API de Unergy no sabe
    de qué planta se le habla."""
    from api.v1.monitoreo.queryset import proyectos_en_operacion

    _proyecto("Con Sub")
    _proyecto("Sin Sub", sub_project=None)

    nombres = [p.nombre_comercial for p in proyectos_en_operacion()]

    assert nombres == ["Con Sub"]


def test_el_selector_sigue_pidiendo_que_este_en_operacion(base_limpia):
    from api.v1.monitoreo.queryset import proyectos_en_operacion

    _proyecto("Operando")
    _proyecto("En Obra", estado="en_construccion")

    nombres = [p.nombre_comercial for p in proyectos_en_operacion()]

    assert nombres == ["Operando"]


def test_build_projects_tampoco_la_devuelve(base_limpia):
    """El endpoint, no solo el queryset: es lo que consume la pantalla."""
    from api.v1.monitoreo.queryset import build_projects

    _proyecto("Planta Viva")
    _borrado("Planta Dada De Baja")

    nombres = [p["nombre_comercial"] for p in build_projects()["projects"]]

    assert nombres == ["Planta Viva"]


# ── La generación de hoy ────────────────────────────────────────────────────


def test_la_generacion_de_hoy_no_incluye_una_planta_borrada(base_limpia):
    from apps.energia.services.solarview_monitoreo import _proyectos_en_operacion

    _proyecto("Planta Viva", project_id_solarview="11")
    _borrado("Planta Dada De Baja", project_id_solarview="22")

    nombres = [p.nombre_comercial for p, _ in _proyectos_en_operacion()]

    assert nombres == ["Planta Viva"]


# ── Las tarjetas de Tiempo Real ─────────────────────────────────────────────


def test_las_tarjetas_no_cuentan_una_planta_borrada(base_limpia):
    """Acá el borrado no solo aparecía: además sumaba en `fleet.total` y en la
    capacidad del encabezado."""
    import inspect

    from apps.energia.services import solarview_monitoreo

    fuente = inspect.getsource(solarview_monitoreo.monitoreo_flota)

    assert "deleted_at__isnull=True" in fuente, (
        "monitoreo_flota volvió a consultar sin excluir los borrados"
    )
