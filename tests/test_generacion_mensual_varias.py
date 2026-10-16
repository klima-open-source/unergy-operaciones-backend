"""`getMonthlyGeneration`: totales por mes de varias plantas en una llamada.

El Ranking vs P90 pedía `getGeneration` por mes y por planta, en serie (un
portafolio de 30 plantas en un año: 360 peticiones). Lo que se prueba es que
una sola consulta por planta, sumada por mes, da lo mismo que pedir mes a mes, y
que el P90 de cada mes sale de la curva de doce valores.
"""
from datetime import date

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
    from django.db import transaction

    from apps.plataforma.models import Usuario

    atomica = transaction.atomic()
    atomica.__enter__()
    usuario = Usuario.objects.create(nombre="QA", email="qa@unergy.io", rol="admin", activo=True)
    yield {"usuario": usuario}
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


# Un contador que sube 10 kWh cada mediodía, del 30 de enero al 2 de marzo.
LECTURAS = [
    {"time_stamp": f"{d} 12:00", "generacion": 10 * i}
    for i, d in enumerate([
        "2026-01-30", "2026-01-31", "2026-02-01", "2026-02-02", "2026-02-28",
        "2026-03-01", "2026-03-02",
    ])
]


@pytest.fixture
def api(monkeypatch):
    from apps.energia.services import unergy_api

    llamadas = []

    def _lecturas(token_, sub_project, desde, hasta):
        llamadas.append(sub_project)
        if sub_project == "rota":
            raise RuntimeError("API caída")
        return [dict(x) for x in LECTURAS], "verificada"

    monkeypatch.setattr(unergy_api, "token", lambda: "tk")
    monkeypatch.setattr(unergy_api, "lecturas_con_respaldo", _lecturas)
    return llamadas


def test_suma_por_mes_con_una_consulta_por_planta(datos, api):
    from api.v1.monitoreo.queryset import build_generacion_mensual
    from apps.proyectos.models import Proyecto

    Proyecto.objects.create(nombre_comercial="A", sub_project="sp-a", p90_mensual_kwh=list(range(1, 13)))

    r = build_generacion_mensual(["sp-a", "sp-b"], date(2026, 2, 1), date(2026, 3, 31))

    assert sorted(api) == ["sp-a", "sp-b"]  # una por planta, no una por mes
    assert r["proyectos"]["sp-a"]["meses"] == {"2026-02": 30.0, "2026-03": 20.0}
    assert r["proyectos"]["sp-a"]["curva_p90_kwh"] == list(range(1, 13))
    assert r["proyectos"]["sp-b"]["curva_p90_kwh"] is None  # sin proyecto en la base


def test_un_mes_da_lo_mismo_que_pedirlo_solo(datos, api):
    """El primer intervalo de marzo usa la última lectura de febrero, igual que
    cuando se pedía marzo aparte (con su margen de dos días)."""
    from api.v1.monitoreo.queryset import build_generacion_mensual, build_generation

    junto = build_generacion_mensual(["sp-a"], date(2026, 2, 1), date(2026, 3, 31))
    solo = build_generation("sp-a", date(2026, 3, 1), date(2026, 3, 31))

    assert junto["proyectos"]["sp-a"]["meses"]["2026-03"] == sum(d["kwh"] for d in solo["data"])


def test_una_planta_que_falla_no_vacia_las_demas(datos, api):
    from api.v1.monitoreo.queryset import build_generacion_mensual

    r = build_generacion_mensual(["rota", "sp-a"], date(2026, 2, 1), date(2026, 2, 28))

    assert r["proyectos"]["rota"] == {"meses": {}, "curva_p90_kwh": None, "fuente": "error"}
    assert r["proyectos"]["sp-a"]["meses"] == {"2026-02": 30.0}
