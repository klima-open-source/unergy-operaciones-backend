"""Comunicación por fuente (`apps/monitoreo/services/comunicacion.py`).

Una fuente (inversores o medidor) está sin comunicación si su último dato llegó
hace más de 2 h, o si hoy no llegó ninguno. Un fallo de la CONSULTA no es una
planta sin comunicación: conserva su estado anterior y, si fallaron todas, se
avisa. Ningún test sale a la red.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

COL = timezone(timedelta(hours=-5))
AHORA = datetime(2026, 10, 5, 14, 0, tzinfo=COL)


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


@pytest.fixture
def com():
    from django.core.cache import cache
    from django.test import override_settings

    from apps.monitoreo.services import comunicacion

    with override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    ):
        cache.clear()
        yield comunicacion


# ── Leer la hora del último dato ──────────────────────────────────────────────


def test_la_hora_de_solarview_viene_sin_zona_y_es_de_bogota(com):
    assert com._hora_bogota("2026-10-05 17:30") == datetime(2026, 10, 5, 17, 30, tzinfo=COL)


def test_la_hora_de_quoia_trae_su_zona_y_se_respeta(com):
    assert com._hora_bogota("2026-10-05T13:00:00-05:00") == datetime(2026, 10, 5, 13, 0, tzinfo=COL)
    assert com._hora_bogota("2026-10-05T18:00:00Z") == datetime(2026, 10, 5, 13, 0, tzinfo=COL)
    assert com._hora_bogota(None) is None
    assert com._hora_bogota("no es una hora") is None


def test_el_ultimo_dato_de_inversores_ignora_los_puntos_que_no_han_llegado(com):
    respuesta = {"results": {"power": {
        "2026-10-05 13:00": 120.0, "2026-10-05 13:05": 0.0, "2026-10-05 13:10": None,
    }}}
    # El 0 kW cuenta: el equipo reportó. El None es un punto que no llegó.
    assert com.ultimo_dato_inversores(respuesta) == datetime(2026, 10, 5, 13, 5, tzinfo=COL)


def test_sin_puntos_hoy_no_hay_ultimo_dato(com):
    """Puya, 2026-10-05: SolarView respondió 200 con la serie vacía."""
    assert com.ultimo_dato_inversores({"results": {"power": {}}}) is None
    assert com.ultimo_dato_medidor(None) is None


# ── Evaluar ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("hace, sin_comunicacion", [
    (timedelta(minutes=30), False),
    (timedelta(hours=2), False),
    (timedelta(hours=2, minutes=1), True),
])
def test_sin_comunicacion_es_mas_de_dos_horas_sin_dato(com, hace, sin_comunicacion):
    assert com.evaluar(AHORA - hace, AHORA)["sin_comunicacion"] is sin_comunicacion


def test_sin_ningun_dato_hoy_es_sin_comunicacion(com):
    assert com.evaluar(None, AHORA) == {"sin_comunicacion": True, "ultimo_dato": None}


def test_registrar_conserva_la_fuente_que_no_se_pudo_consultar(com):
    com.registrar({1: {"inversores": com.evaluar(AHORA, AHORA),
                       "medidor": com.evaluar(None, AHORA)}}, AHORA)
    # Siguiente corrida: el medidor se consultó, los inversores fallaron.
    com.registrar({1: {"medidor": com.evaluar(AHORA, AHORA)}}, AHORA + timedelta(minutes=15))

    fila = com.leer()[1]
    assert fila["inversores"]["sin_comunicacion"] is False  # lo anterior
    assert fila["medidor"]["sin_comunicacion"] is False      # lo nuevo


# ── Con lo que pidió el sondeo ────────────────────────────────────────────────


def _registrar(power_map, snap_map, node_pairs, gaia_activo=True):
    from apps.monitoreo.services.alarmas import desconexion

    proyectos = [SimpleNamespace(id=pid) for pid in node_pairs]
    desconexion._registrar_comunicacion(proyectos, node_pairs, power_map, snap_map,
                                        gaia_activo=gaia_activo)


def test_el_sondeo_registra_cada_fuente_por_separado(com, monkeypatch):
    from apps.plataforma.services import fechas

    monkeypatch.setattr(fechas, "ahora_col", lambda: AHORA)
    _registrar(
        power_map={1: {"results": {"power": {}}}, 2: {"results": {"power": {"2026-10-05 13:50": 3.0}}}},
        snap_map={1: {"last_time": "2026-10-05T13:45:00-05:00"}, 2: None},
        node_pairs={1: (10, 11), 2: (None, None)},
    )

    estado = com.leer()
    # Puya: inversores vacíos, medidor al día.
    assert estado[1]["inversores"]["sin_comunicacion"] is True
    assert estado[1]["medidor"]["sin_comunicacion"] is False
    # Sin medidor vinculado: no hay fuente, no "sin comunicación".
    assert estado[2]["inversores"]["sin_comunicacion"] is False
    assert estado[2]["medidor"] is None


def test_una_consulta_que_falla_no_marca_la_planta(com, monkeypatch):
    from apps.plataforma.services import fechas

    monkeypatch.setattr(fechas, "ahora_col", lambda: AHORA)
    _registrar(power_map={1: "ERROR"}, snap_map={1: "ERROR"}, node_pairs={1: (10, None)})

    assert 1 not in com.leer()


def test_si_fallan_todas_las_consultas_de_una_fuente_se_avisa(com, monkeypatch):
    from apps.plataforma.services import fechas

    monkeypatch.setattr(fechas, "ahora_col", lambda: AHORA)
    _registrar(
        power_map={1: "ERROR", 2: "ERROR"},
        snap_map={1: {"last_time": "2026-10-05T13:45:00-05:00"}, 2: "ERROR"},
        node_pairs={1: (10, None), 2: (20, None)},
    )

    consultas = com.consultas()
    assert consultas["inversores"]["fallo"] is True   # todas fallaron
    assert consultas["medidor"]["fallo"] is False     # una respondió
    assert consultas["inversores"]["consultado_en"] == AHORA.isoformat()


def test_la_flota_ordena_primero_las_que_no_comunican_por_ninguna_fuente():
    from apps.energia.services.solarview_monitoreo import _orden_comunicacion

    nada = {"comunicacion": {"inversores": {"sin_comunicacion": True},
                             "medidor": {"sin_comunicacion": True}}}
    una = {"comunicacion": {"inversores": {"sin_comunicacion": True}, "medidor": None}}
    bien = {"comunicacion": {"inversores": {"sin_comunicacion": False}, "medidor": None}}
    sin_evaluar = {"comunicacion": None}

    orden = sorted([bien, sin_evaluar, una, nada], key=_orden_comunicacion)
    assert orden[:2] == [nada, una]
