"""Alertas de aniversario de Representación/CGM: de dónde sale el IPC.

El servicio traía las tasas en un dict escrito a mano
(`{2023: 0.0928, 2024: 0.052, 2025: 0.051}`) con un `IPC_POR_DEFECTO = 0.051`
para todo lo que cayera fuera. Dos consecuencias que nadie veía:

  * cualquier aniversario de 2027 en adelante se indexaba al 5,1% inventado, sin
    decir que era un supuesto;
  * la tarifa se capitalizaba con UNA sola tasa elevada a la cantidad de
    aniversarios (`tarifa * (1 + ipc) ** numero`), así que un contrato de 2023
    —cuyos años reales fueron 9,28%, 5,20% y 5,10%— salía calculado como 5,10³.

Ahora las tasas salen de `om_ipc_tasas`, que es la única con mantenimiento
(la tarea `om.revisar_ipc_del_anio` le crea la fila cada 1-enero y hay pantalla
para confirmarla). Esa tabla indexa por año de APLICACIÓN: la fila 2026 guarda
el IPC de dic-2025, que es el que indexa un aniversario de 2026. De ahí que la
búsqueda sea directa y el año que se MUESTRA sea el anterior.

Sin `pytest-django`: el fixture arma sqlite en memoria con `create_test_db`,
igual que `tests/test_fallas_django.py` — ver su docstring para el detalle de
por qué hay que invalidar el cache del `ConnectionHandler`.
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


# Los tres años con tasa real, por año de APLICACIÓN (como los guarda la tabla).
TASAS = {2024: 0.0928, 2025: 0.052, 2026: 0.051}


# ── De qué año sale la tasa ───────────────────────────────────────────────────

def test_ipc_de_toma_la_tasa_del_anio_del_aniversario():
    """La fila 2026 es la que indexa el aniversario de 2026 (búsqueda directa)."""
    from apps.contratos.services.alertas_representacion import ipc_de

    assert ipc_de(2026, TASAS) == (0.051, 2025)


def test_ipc_de_muestra_el_anio_anterior():
    """El número es el IPC de dic-2025 aunque se aplique en 2026: se etiqueta 2025."""
    from apps.contratos.services.alertas_representacion import ipc_de

    _, anio_mostrado = ipc_de(2026, TASAS)
    assert anio_mostrado == 2025


def test_ipc_de_es_none_si_nadie_confirmo_la_tasa():
    """Antes devolvía 0.051 inventado; ahora dice que no sabe."""
    from apps.contratos.services.alertas_representacion import ipc_de

    assert ipc_de(2027, TASAS) == (None, 2026)


# ── Encadenado por año ────────────────────────────────────────────────────────

def test_factor_encadena_la_tasa_de_cada_anio():
    """Tres aniversarios (2024, 2025, 2026) → producto de las tres tasas."""
    from apps.contratos.services.alertas_representacion import factor_ipc

    factor, faltantes = factor_ipc(2026, 3, TASAS)
    assert factor == pytest.approx(1.0928 * 1.052 * 1.051)
    assert faltantes == []


def test_factor_reporta_los_anios_sin_tasa():
    from apps.contratos.services.alertas_representacion import factor_ipc

    factor, faltantes = factor_ipc(2026, 3, {2026: 0.051})
    assert factor == pytest.approx(1.051)
    assert faltantes == [2024, 2025]


def test_tarifa_usa_cada_tasa_y_no_una_elevada():
    """5,0 con años 9,28/5,20/5,10 da 6,0413 — no 5,8047 (que es 5·1,051³)."""
    from apps.contratos.services.alertas_representacion import tarifa_indexada

    assert tarifa_indexada(5.0, 2026, 3, TASAS) == 6.0413


def test_tarifa_es_none_si_falta_la_tasa_del_propio_aniversario():
    """La tarifa nueva ES la del aniversario que se avisa: sin esa tasa no hay
    número que dar, aunque se conozcan las de años anteriores."""
    from apps.contratos.services.alertas_representacion import tarifa_indexada

    assert tarifa_indexada(5.0, 2030, 2, TASAS) is None   # ni 2029 ni 2030
    assert tarifa_indexada(5.0, 2030, 6, TASAS) is None   # 2025/2026 sí, 2030 no


def test_tarifa_sale_aunque_falten_anios_viejos():
    """Con la tasa del aniversario cargada el número se puede dar; los años
    viejos que falten se avisan aparte como proyección parcial."""
    from apps.contratos.services.alertas_representacion import tarifa_indexada

    assert tarifa_indexada(5.0, 2026, 3, {2026: 0.051}) == 5.255


def test_tarifa_es_none_sin_tarifa_base():
    from apps.contratos.services.alertas_representacion import tarifa_indexada

    assert tarifa_indexada(None, 2026, 1, TASAS) is None
    assert tarifa_indexada(0, 2026, 1, TASAS) is None


# ── Lo que dice el correo ─────────────────────────────────────────────────────

CONTRATO = {
    "firma": None,
    "proyecto": "Minigranja Solar Uruaco",
    "inversionista": "Patrimonio Autónomo",
    "tarifa_cgm": 5.0,
    "tarifa_representacion": 5.0,
}


def _html(anio: int, numero: int, tasas: dict[int, float]) -> str:
    from datetime import date

    from apps.contratos.services.alertas_representacion import construir_html

    return construir_html(CONTRATO, date(anio, 6, 15), numero, 30, tasas)


def test_correo_etiqueta_el_ipc_con_el_anio_anterior():
    html = _html(2026, 3, TASAS)
    assert "5.10%" in html
    assert "IPC dic 2025" in html


def test_correo_avisa_igual_sin_tasa_pero_no_inventa_tarifa():
    """La alerta del aniversario sale; la tarifa se omite y se dice por qué."""
    html = _html(2030, 6, TASAS)
    assert "pendiente de confirmación" in html
    assert "Nueva tarifa" not in html
    assert "se cumple el aniversario" in html


def test_correo_marca_la_proyeccion_parcial():
    """Con solo una de las tres tasas, el número sale pero se avisa que es parcial."""
    html = _html(2026, 3, {2026: 0.051})
    assert "Nueva tarifa" in html
    assert "parcial" in html.lower()
    assert "2024" in html and "2025" in html


def test_correo_completo_no_habla_de_parcial():
    html = _html(2026, 3, TASAS)
    assert "parcial" not in html.lower()


# ── Aniversarios ──────────────────────────────────────────────────────────────

def test_29_de_febrero_cae_al_28_en_anio_no_bisiesto():
    from datetime import date

    from apps.contratos.services.alertas_representacion import proximo_aniversario

    aniversario, numero = proximo_aniversario(date(2024, 2, 29), date(2025, 1, 1))
    assert (aniversario, numero) == (date(2025, 2, 28), 1)


# ── La lectura de la tabla ────────────────────────────────────────────────────

def test_solo_entran_las_tasas_confirmadas():
    """La tarea del 1-enero crea la fila en 0,0 con `confirmado=False`. Leerla sin
    filtrar aplicaría 0% de indexación en silencio, que es peor que no saber."""
    from django.db import transaction

    from apps.contratos.services.alertas_representacion import tasas_ipc_confirmadas
    from apps.om.models import OmIpcTasa

    with transaction.atomic():
        OmIpcTasa.objects.create(año=2025, tasa="0.052000", confirmado=True, fuente="DANE")
        OmIpcTasa.objects.create(año=2026, tasa="0.051000", confirmado=True, fuente="DANE")
        OmIpcTasa.objects.create(año=2027, tasa="0.000000", confirmado=False,
                                 fuente="pendiente_confirmacion")

        assert tasas_ipc_confirmadas() == {2025: 0.052, 2026: 0.051}

        transaction.set_rollback(True)


