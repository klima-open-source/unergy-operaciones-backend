"""La sincronizacion de Sun Factory no pisa la potencia que cargo el operador.

`sync_tsf_projects` enlaza cada proyecto de Sun Factory con el nuestro y rellena
lo que falte. Su propio comentario dice la regla: "COALESCE(existente, nuevo):
enlaza y rellena sin pisar lo que el operador ya tenga", y ocho de los diez
campos la cumplen. `potencia_ac_kw` estaba al reves --el valor de Sun
Factory ganaba-- asi que cada corrida deshacia una correccion manual.

Importa mas de lo que parece porque esa columna, pese al nombre, guarda la
potencia AC (verificado en produccion el 2026-09-10: coincide con
`proyecto_info_tecnica.potencia_ac_kw` en 101 de 101 proyectos), mientras el
campo que llega se llama `installed_power_kwp`. Si Sun Factory mandara ahi una
capacidad DC, esta linea la escribiria como AC -- el mismo error que ya se
corrigio dos veces en otros lados (edicion manual, fix del 2026-08-19; backfill
de Solenium, que directamente prefiere dejarlo vacio antes que escribir un DC
mal etiquetado).

`avance_obra_pct` se queda como estaba, a proposito: es una medicion viva del
avance de obra, nadie la carga a mano, y congelarla en su primer valor dejaria
la vista de "Proximos a energizar" mostrando un porcentaje viejo para siempre.

Las pruebas de esta sincronizacion (tests/test_proximos_energizar.py) apuntan a
`app/services/tsf_sync.py`, el arbol FastAPI apagado. Esta prueba mira el codigo
que de verdad corre.
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


def _proyecto_de_sunfactory(**kw):
    """Un proyecto nuestro ya enlazado con Sun Factory."""
    from apps.proyectos.models import Proyecto

    kw.setdefault("nombre_comercial", "Morroa Sur")
    kw.setdefault("origina_code", "COLSUCT3P1_MORROA_SUR")
    kw.setdefault("codigo_tsf", "COLSUCT3P1")
    kw.setdefault("sunfactory_project_id", "106")
    kw.setdefault("estado", "en_desarrollo")
    return Proyecto.objects.create(**kw)


def _payload(**kw):
    """Lo que devuelve `fetch_sunfactory_projects` para ese proyecto."""
    from datetime import date

    fila = {
        "origina_code": "COLSUCT3P1_MORROA_SUR",
        "base_name": "COLSUCT3P1_MORROA_SUR",
        "tsf_code": "COLSUCT3P1",
        "commercial_name": "Morroa Sur",
        "status": "Próximo a energizar",
        "stage": "deploy",
        "energization_date": date(2026, 9, 1),
        "energization_source": "sunfactory",
        "avance_pct": 88.5,
        "monthly_mwh": 127.71,
        "installed_power_kwp": 990,
        "already_generating": False,
        "municipio": "Morroa",
        "departamento": "Sucre",
        "latitud": None,
        "longitud": None,
        "solenium_id": 106,
    }
    fila.update(kw)
    return [fila]


def _sincronizar(monkeypatch, filas):
    from apps.proyectos.services import tsf_sync

    monkeypatch.setattr(
        tsf_sync, "fetch_sunfactory_projects", lambda **k: (filas, []),
    )
    return tsf_sync.sync_tsf_projects()


def test_no_pisa_la_potencia_que_ya_tenia_el_proyecto(base_limpia, monkeypatch):
    """El caso real: alguien corrige la potencia a mano y la siguiente corrida
    de la sincronizacion la devolvia al valor de Sun Factory."""
    from apps.proyectos.models import Proyecto

    proyecto = _proyecto_de_sunfactory(potencia_ac_kw=1300)

    stats = _sincronizar(monkeypatch, _payload(installed_power_kwp=990))

    assert stats["actualizados"] == 1
    assert float(Proyecto.objects.get(pk=proyecto.id).potencia_ac_kw) == 1300.0


def test_rellena_la_potencia_cuando_esta_vacia(base_limpia, monkeypatch):
    """Rellenar sigue siendo el trabajo de la sincronizacion: lo que no puede es
    pisar."""
    from apps.proyectos.models import Proyecto

    proyecto = _proyecto_de_sunfactory(potencia_ac_kw=None)

    _sincronizar(monkeypatch, _payload(installed_power_kwp=990))

    assert float(Proyecto.objects.get(pk=proyecto.id).potencia_ac_kw) == 990.0


def test_una_potencia_ausente_en_sun_factory_no_borra_la_nuestra(base_limpia, monkeypatch):
    from apps.proyectos.models import Proyecto

    proyecto = _proyecto_de_sunfactory(potencia_ac_kw=1300)

    _sincronizar(monkeypatch, _payload(installed_power_kwp=None))

    assert float(Proyecto.objects.get(pk=proyecto.id).potencia_ac_kw) == 1300.0


def test_el_avance_de_obra_si_se_actualiza(base_limpia, monkeypatch):
    """La excepcion deliberada: el avance es una medicion viva y tiene que
    seguir a Sun Factory, o la vista de proximos a energizar se queda con un
    porcentaje viejo para siempre."""
    from apps.proyectos.models import Proyecto

    proyecto = _proyecto_de_sunfactory(avance_obra_pct=40)

    _sincronizar(monkeypatch, _payload(avance_pct=88.5))

    assert float(Proyecto.objects.get(pk=proyecto.id).avance_obra_pct) == 88.5


def test_los_demas_campos_siguen_sin_pisarse(base_limpia, monkeypatch):
    """Municipio y departamento ya respetaban lo cargado; la prueba los fija
    para que el arreglo de la potencia no se lea como un caso especial."""
    from apps.proyectos.models import Proyecto

    proyecto = _proyecto_de_sunfactory(municipio="Sincelejo", departamento="Sucre")

    _sincronizar(monkeypatch, _payload(municipio="Morroa", departamento="Otro"))

    guardado = Proyecto.objects.get(pk=proyecto.id)
    assert guardado.municipio == "Sincelejo"
    assert guardado.departamento == "Sucre"
