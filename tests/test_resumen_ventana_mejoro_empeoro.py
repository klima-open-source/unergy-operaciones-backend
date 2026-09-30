"""El umbral de "mejoró"/"empeoró" es en DÍAS, no en puntos porcentuales.

Un mismo umbral en % no significa lo mismo en una ventana de 7 días (1 día ya
son ~14 pts) que en una de 30 (1 día son ~3 pts) -- por eso `resumen_ventana`
compara directamente cuántos días automáticos más o menos tuvo la ventana
actual contra la anterior de igual duración, y el corte es "más de 2 días".
Acá se fija el límite exacto: 2 no cuenta, 3 sí.
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

# Ventana actual: 7 días. La anterior de igual duración es automática --
# `resumen_ventana` la calcula sola, contigua hacia atrás.
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
    """Una segunda frontera que SIEMPRE sale por CGM, los 14 días de las dos
    ventanas. `_motivos_del_rango` marca el día ENTERO como
    `clasificacion_fallida` si NINGUNA frontera usó CGM ese día -- con una
    sola frontera en el escenario, cualquier día "principal" dispararía esa
    regla y el día quedaría excluido en vez de contar como no-automático. La
    regla está pensada para ~145 fronteras reales (un cero absoluto ahí sí es
    el clasificador fallando); acá alcanza con una segunda frontera de control
    para que el escenario no dependa de esa exención."""
    f = _frontera("Ancla")
    for n in range(7):
        _reporte(f, _dia_previo(n), "cgm")
        _reporte(f, _dia_actual(n), "cgm")
    return f


def _sembrar(frontera, dias_automaticos_previo, dias_automaticos_actual):
    """7 días en cada ventana; los primeros `dias_automaticos_*` por CGM, el
    resto por medidor -- para no depender de qué fuente ganó, solo del
    conteo."""
    for n in range(7):
        _reporte(frontera, _dia_previo(n), "cgm" if n < dias_automaticos_previo else "principal")
        _reporte(frontera, _dia_actual(n), "cgm" if n < dias_automaticos_actual else "principal")


def _fila(frontera_id):
    from apps.energia.services.reporte.vistas import resumen_ventana

    r = resumen_ventana(DESDE, HASTA)
    fila = next(f for f in r["filas"] if f["frontera_id"] == frontera_id)
    return fila, r["kpis"]


def test_delta_de_exactamente_2_dias_no_cuenta(base_limpia):
    _ancla()
    f = _frontera("Frontera")
    _sembrar(f, dias_automaticos_previo=2, dias_automaticos_actual=4)  # +2

    fila, kpis = _fila(f.id)

    assert fila["dias_automaticos"] == 4
    assert kpis["mejoraron"] == 0
    assert kpis["empeoraron"] == 0
    assert fila["cambio"] is None


def test_delta_de_exactamente_3_dias_si_cuenta_como_mejoro(base_limpia):
    _ancla()
    f = _frontera("Frontera")
    _sembrar(f, dias_automaticos_previo=2, dias_automaticos_actual=5)  # +3

    fila, kpis = _fila(f.id)

    assert kpis["mejoraron"] == 1
    assert kpis["empeoraron"] == 0
    assert fila["cambio"] == "mejoro"


def test_delta_de_exactamente_menos_3_dias_cuenta_como_empeoro(base_limpia):
    _ancla()
    f = _frontera("Frontera")
    _sembrar(f, dias_automaticos_previo=5, dias_automaticos_actual=2)  # -3

    fila, kpis = _fila(f.id)

    assert kpis["empeoraron"] == 1
    assert kpis["mejoraron"] == 0
    assert fila["cambio"] == "empeoro"


def test_delta_de_exactamente_menos_2_dias_no_cuenta(base_limpia):
    _ancla()
    f = _frontera("Frontera")
    _sembrar(f, dias_automaticos_previo=5, dias_automaticos_actual=3)  # -2

    _, kpis = _fila(f.id)

    assert kpis["empeoraron"] == 0
    assert kpis["mejoraron"] == 0


def test_sin_historial_previo_no_cuenta_como_empeoro(base_limpia):
    """Una frontera nueva, sin ningún día en la ventana anterior, no tiene
    base contra la cual comparar -- no es "empeoró", es que no había nada
    antes."""
    _ancla()
    f = _frontera("Nueva")
    for n in range(7):
        _reporte(f, _dia_actual(n), "principal")  # 0 automáticos, sin previo

    _, kpis = _fila(f.id)

    assert kpis["mejoraron"] == 0
    assert kpis["empeoraron"] == 0


def test_las_filas_marcadas_son_las_que_cuentan_las_tarjetas(base_limpia):
    """El frontend filtra la tabla por `cambio` al hacer clic en "Mejoraron" o
    "Empeoraron": las filas que quedan tienen que ser tantas como dice la
    tarjeta, ni una más."""
    from apps.energia.services.reporte.vistas import resumen_ventana

    _ancla()
    _sembrar(_frontera("Sube"), dias_automaticos_previo=1, dias_automaticos_actual=6)
    _sembrar(_frontera("Baja"), dias_automaticos_previo=6, dias_automaticos_actual=1)
    _sembrar(_frontera("Quieta"), dias_automaticos_previo=3, dias_automaticos_actual=4)

    r = resumen_ventana(DESDE, HASTA)
    cambios = [f["cambio"] for f in r["filas"]]

    assert cambios.count("mejoro") == r["kpis"]["mejoraron"] == 1
    assert cambios.count("empeoro") == r["kpis"]["empeoraron"] == 1
