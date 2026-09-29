"""Cuando una frontera usó más de una fuente manual en la ventana.

`resumen_ventana` reemplaza los tres gráficos separados del Resumen viejo por
una sola fila por frontera+tipo. Esa fila necesita UNA fuente para mostrar
(con un "+N más" al lado en el frontend), aunque la frontera haya usado varias
distintas durante la semana o el mes -- acá se fija cuál gana y cómo se arma
el desglose completo.
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


from datetime import date, timedelta  # noqa: E402

DESDE = date(2026, 9, 1)


def _dia(n: int) -> date:
    return DESDE + timedelta(days=n)


def _frontera(nombre="Frontera"):
    from apps.fronteras.models import Frontera

    return Frontera.objects.create(
        nombre_frontera=nombre, codigo_frontera=nombre.lower(), estado="activa",
        fecha_registro_asic=DESDE, deleted_at=None,
    )


def _reporte(frontera, dia, medidor_usado):
    from apps.energia.models import ReporteEnergiaGeneracion

    return ReporteEnergiaGeneracion.objects.create(
        frontera_id=frontera.id, fecha=dia, medidor_usado=medidor_usado, caso="1",
    )


def _ancla():
    """Segunda frontera que sale por CGM todos los días -- sin ella, un día
    "principal"/"historico" de la única frontera del escenario dispararía la
    regla de "clasificación fallida" (cero absoluto de CGM ese día) y quedaría
    excluido en vez de contar como no-automático. Ver el comentario igual en
    `test_resumen_ventana_mejoro_empeoro.py`."""
    f = _frontera("Ancla")
    for dia in range(7):
        _reporte(f, _dia(dia), "cgm")
    return f


def _fila(frontera_id):
    from apps.energia.services.reporte.vistas import resumen_ventana

    r = resumen_ventana(DESDE, _dia(6), None)
    return next(f for f in r["filas"] if f["frontera_id"] == frontera_id)


def test_la_fuente_con_mas_dias_gana(base_limpia):
    """4 días Medidor ('principal') + 2 días Estimación ('historico')."""
    _ancla()
    f = _frontera()
    for dia, fuente in [
        (0, "principal"), (1, "principal"), (2, "principal"), (3, "principal"),
        (4, "historico"), (5, "historico"),
        (6, "cgm"),
    ]:
        _reporte(f, _dia(dia), fuente)

    fila = _fila(f.id)

    assert fila["fuente_dominante"] == "medidor"
    assert fila["fuente_dominante_etiqueta"] == "Medidor"
    assert fila["desglose_fuente"] == [
        {"grupo": "medidor", "etiqueta": "Medidor", "dias": 4},
        {"grupo": "estimacion", "etiqueta": "Estimación", "dias": 2},
    ]
    assert fila["dias_automaticos"] == 1
    assert fila["dias_no_automaticos"] == 6


def test_una_sola_fuente_no_trae_desglose_de_mas(base_limpia):
    _ancla()
    f = _frontera()
    for dia in range(7):
        _reporte(f, _dia(dia), "principal")

    fila = _fila(f.id)

    assert fila["desglose_fuente"] == [{"grupo": "medidor", "etiqueta": "Medidor", "dias": 7}]


def test_el_empate_lo_desempata_el_orden_de_confianza(base_limpia):
    """3 y 3: gana Medidor porque `_ORDEN_GRUPO_FUENTE` lo pone antes que
    Estimacion -- mismo desempate que ya usaban los graficos viejos."""
    _ancla()
    f = _frontera()
    for dia, fuente in [
        (0, "principal"), (1, "principal"), (2, "principal"),
        (3, "historico"), (4, "historico"), (5, "historico"),
        (6, "cgm"),
    ]:
        _reporte(f, _dia(dia), fuente)

    fila = _fila(f.id)

    assert fila["fuente_dominante"] == "medidor"


def test_la_fila_trae_el_codigo_de_la_frontera(base_limpia):
    """La tabla muestra el código ASIC (`Frt0103697`) debajo del proyecto: dos
    fronteras del mismo proyecto (generación y consumo) solo se distinguen por
    él."""
    _ancla()
    f = _frontera("Garza")
    _reporte(f, _dia(0), "principal")

    assert _fila(f.id)["codigo_frontera"] == "garza"
