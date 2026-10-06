""""Nunca reportan automatico" en el Resumen: dos casos muy distintos que
terminan en el mismo balde.

Una frontera puede tener 0% de dias automaticos con datos reales (corrio,
pero siempre a mano), o puede no tener NINGUN dato clasificado en la ventana
(dejo de aparecer -- el mismo patron que ya detecta
`_fronteras_registradas_por_dia`, pero a nivel de ventana). Las dos cuentan
como "nunca automatico" para el KPI, y la segunda ademas se marca
`nunca_clasificado` y ordena primero en la tabla.
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

DESDE = date(2026, 9, 8)
HASTA = DESDE + timedelta(days=6)
PREVIO_DESDE = DESDE - timedelta(days=7)


def _dia_actual(n: int) -> date:
    return DESDE + timedelta(days=n)


def _dia_previo(n: int) -> date:
    return PREVIO_DESDE + timedelta(days=n)


def _frontera(nombre):
    from apps.fronteras.models import Frontera

    return Frontera.objects.create(
        nombre_frontera=nombre, codigo_frontera=nombre.lower(), estado="activa",
        fecha_registro_asic=PREVIO_DESDE, deleted_at=None,
    )


def _reporte(frontera, dia, medidor_usado):
    from apps.energia.models import ReporteEnergiaGeneracion

    return ReporteEnergiaGeneracion.objects.create(
        frontera_id=frontera.id, fecha=dia, medidor_usado=medidor_usado, caso="1",
    )


def _ancla():
    """Ver el comentario en test_resumen_ventana_mejoro_empeoro.py: una
    frontera de control que siempre sale por CGM, para que un dia sin CGM de
    las otras no dispare la regla de "clasificacion fallida" (pensada para
    ~145 fronteras, no para un escenario de una sola)."""
    f = _frontera("Ancla")
    for n in range(7):
        _reporte(f, _dia_previo(n), "cgm")
        _reporte(f, _dia_actual(n), "cgm")
    return f


def _ventana():
    from apps.energia.services.reporte.vistas import resumen_ventana

    return resumen_ventana(DESDE, HASTA)


def test_cero_por_ciento_con_datos_reales_cuenta_como_nunca(base_limpia):
    """Corrio los 7 dias, siempre a mano -- tiene datos, tasa 0%."""
    _ancla()
    f = _frontera("Manual")
    for n in range(7):
        _reporte(f, _dia_actual(n), "principal")

    r = _ventana()
    fila = next(x for x in r["filas"] if x["frontera_id"] == f.id)

    assert fila["nunca_clasificado"] is False
    assert fila["tasa"] == 0.0
    assert r["kpis"]["nunca_automatico"] >= 1


def test_cero_filas_en_la_ventana_actual_se_marca_nunca_clasificado(base_limpia):
    """Tenia datos la ventana anterior y desaparecio en la actual -- el mismo
    patron de fronteras que se borran sin que nadie lo note."""
    _ancla()
    f = _frontera("Desaparecida")
    for n in range(7):
        _reporte(f, _dia_previo(n), "principal")
    # nada en la ventana actual

    r = _ventana()
    fila = next(x for x in r["filas"] if x["frontera_id"] == f.id)

    assert fila["nunca_clasificado"] is True
    assert fila["dias_automaticos"] == 0
    assert fila["dias_no_automaticos"] == 0


def test_sin_ninguna_fila_en_ninguna_ventana_no_aparece(base_limpia):
    """Una frontera sin historial en NINGUNA de las dos ventanas no es un dato
    nuestro -- no sale en la tabla en absoluto (no hay nada de que alarmar)."""
    _ancla()
    otra = _frontera("Sin historial")

    r = _ventana()

    assert not any(x["frontera_id"] == otra.id for x in r["filas"])


def test_nunca_clasificado_ordena_primero(base_limpia):
    _ancla()
    normal = _frontera("Normal")
    for n in range(7):
        _reporte(normal, _dia_actual(n), "cgm" if n < 3 else "principal")

    desaparecida = _frontera("Desaparecida")
    for n in range(7):
        _reporte(desaparecida, _dia_previo(n), "principal")

    r = _ventana()

    assert r["filas"][0]["frontera_id"] == desaparecida.id
    assert r["filas"][0]["nunca_clasificado"] is True


def test_no_duplica_el_conteo_entre_los_dos_casos(base_limpia):
    """0% con datos y "nunca clasificado" van al mismo balde del KPI, pero
    cada fila cuenta una sola vez."""
    _ancla()
    manual = _frontera("Manual")
    for n in range(7):
        _reporte(manual, _dia_actual(n), "principal")

    desaparecida = _frontera("Desaparecida")
    for n in range(7):
        _reporte(desaparecida, _dia_previo(n), "principal")

    r = _ventana()

    # Ancla (100%) + Manual (0%, con datos) + Desaparecida (nunca clasificada).
    assert r["kpis"]["nunca_automatico"] == 2
    assert r["kpis"]["siempre_automatico"] == 1
