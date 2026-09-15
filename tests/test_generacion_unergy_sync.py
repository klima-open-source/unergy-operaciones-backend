"""`generacion_diaria` se llena desde la API de Unergy, no desde Solenium.

El sync leía de Solenium y **dejó de escribir el 6 de septiembre de 2026**:
nueve días sin datos en una tabla que alimenta el dashboard, el cumplimiento PPA
y el pipeline comercial. Un cumplimiento cortado se lee como "generó poco", no
como "no se midió" -- peor que no tener nada.

Se compararon las tres fuentes disponibles:

    Solenium         57 proyectos con id    detenida
    SolarView        37 proyectos con id    viva, pero cubre MENOS
    API de Unergy   ~90 con `sub_project`   viva, verificada el 2026-09-15

Lo que se fija acá son las tres cosas que hacen que este sync sea correcto y no
solo funcional:

  · **La API devuelve un CONTADOR ACUMULADO**, no energía por día. `deltas` lo
    convierte, y `_kwh_por_dia` suma esos intervalos por fecha. Si alguien
    tratara la respuesta como kWh directos, los números saldrían disparatados.
  · **Los días en cero se descartan.** Un cero no distingue "no generó" de "no
    reportó", y escribirlo pisaría un dato bueno de otra fuente.
  · **No se pisan filas de otra fuente.** Las de Solenium se quedan intactas y
    las nuevas llenan los huecos: no se reescribe historia.
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


# ── La conversión de intervalos a días ──────────────────────────────────────


def _por_dia(entradas):
    from apps.proyectos.services.generacion_unergy import _kwh_por_dia

    return _kwh_por_dia(entradas)


def test_suma_los_intervalos_de_cada_dia():
    """`deltas` devuelve un punto por lectura, varios por día."""
    entradas = [
        {"date": "2026-09-10", "kwh": 10.0},
        {"date": "2026-09-10", "kwh": 5.5},
        {"date": "2026-09-11", "kwh": 7.25},
    ]

    assert _por_dia(entradas) == [("2026-09-10", 15.5), ("2026-09-11", 7.25)]


def test_descarta_los_dias_en_cero():
    """Un cero no distingue "no generó" de "no reportó", y escribirlo pisaría
    un dato bueno de otra fuente."""
    entradas = [{"date": "2026-09-10", "kwh": 0.0}, {"date": "2026-09-11", "kwh": 3.0}]

    assert _por_dia(entradas) == [("2026-09-11", 3.0)]


def test_devuelve_los_dias_ordenados():
    entradas = [{"date": "2026-09-12", "kwh": 1}, {"date": "2026-09-10", "kwh": 2}]

    assert [f for f, _ in _por_dia(entradas)] == ["2026-09-10", "2026-09-12"]


@pytest.mark.parametrize("basura", [
    [], None,
    [{"date": None, "kwh": 5}],
    [{"date": "2026-09-10", "kwh": None}],
    [{"date": "2026-09-10", "kwh": "no es un número"}],
    [None],
])
def test_los_datos_malos_no_revientan(basura):
    """Mejor un día sin dato que una corrida entera caída."""
    assert _por_dia(basura) == []


# ── Que no pise otras fuentes ───────────────────────────────────────────────


def _proyecto(nombre="Planta", **kw):
    from apps.proyectos.models import Proyecto

    campos = {"nombre_comercial": nombre, "estado": "en_operacion",
              "sub_project": nombre.lower()}
    campos.update(kw)
    return Proyecto.objects.create(**campos)


def _persistir(proyecto_id, dias):
    from apps.proyectos.services.generacion_unergy import _persistir

    return _persistir(proyecto_id, dias)


def _fila(proyecto_id, fecha):
    from apps.proyectos.models import GeneracionDiaria

    return GeneracionDiaria.objects.filter(proyecto_id=proyecto_id, fecha=fecha).first()


def test_crea_los_dias_que_no_existen(base_limpia):
    p = _proyecto()

    assert _persistir(p.id, [("2026-09-10", 42.0)]) == 1
    fila = _fila(p.id, "2026-09-10")
    assert float(fila.kwh_real) == 42.0
    assert fila.fuente == "unergy"


def test_no_pisa_una_fila_de_solenium(base_limpia):
    """El hueco se llena, la historia no se reescribe."""
    from apps.proyectos.models import GeneracionDiaria

    p = _proyecto()
    GeneracionDiaria.objects.create(
        proyecto_id=p.id, fecha="2026-09-01", kwh_real=100, fuente="solenium")

    _persistir(p.id, [("2026-09-01", 999.0)])

    fila = _fila(p.id, "2026-09-01")
    assert float(fila.kwh_real) == 100.0, "pisó un dato de otra fuente"
    assert fila.fuente == "solenium"


def test_no_pisa_una_carga_manual(base_limpia):
    from apps.proyectos.models import GeneracionDiaria

    p = _proyecto()
    GeneracionDiaria.objects.create(
        proyecto_id=p.id, fecha="2026-09-01", kwh_real=77, fuente="manual")

    _persistir(p.id, [("2026-09-01", 999.0)])

    assert float(_fila(p.id, "2026-09-01").kwh_real) == 77.0


def test_si_actualiza_lo_suyo(base_limpia):
    """La ventana móvil existe para esto: la API corrige hacia atrás y un día
    incompleto se completa en una corrida posterior."""
    p = _proyecto()
    _persistir(p.id, [("2026-09-10", 10.0)])

    _persistir(p.id, [("2026-09-10", 25.0)])

    assert float(_fila(p.id, "2026-09-10").kwh_real) == 25.0


# ── El cableado ─────────────────────────────────────────────────────────────


def test_la_tarea_ya_no_lleva_el_nombre_de_la_fuente():
    """Se llamaba `sincronizar_generacion_solenium`. Atar el nombre de la tarea
    a la fuente obliga a renombrarla --y a tocar el horario y
    django_celery_beat-- cada vez que la fuente cambia."""
    from config.horarios import HORARIOS

    tareas = {h["task"] for h in HORARIOS.values()}

    assert "proyectos.sincronizar_generacion_diaria" in tareas
    assert "proyectos.sincronizar_generacion_solenium" not in tareas


def test_las_dos_franjas_siguen_apuntando_a_la_tarea():
    from config.horarios import HORARIOS

    assert HORARIOS["gen-sync-am"]["task"] == "proyectos.sincronizar_generacion_diaria"
    assert HORARIOS["gen-sync-pm"]["task"] == "proyectos.sincronizar_generacion_diaria"


def test_la_tarea_llama_al_sync_de_unergy():
    import inspect

    from apps.proyectos import tasks

    fuente = inspect.getsource(tasks.sincronizar_generacion_diaria)

    assert "generacion_unergy import sincronizar" in fuente


def test_el_sync_de_solenium_ya_no_existe():
    """Se borró en el mismo commit. Dejarlo seria codigo que parece vivo -- el
    mismo error que costo una hora hoy con las vistas huerfanas."""
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("apps.proyectos.services.generacion_solenium")
