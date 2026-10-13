"""El remanente en bolsa se parte por la vigencia GESCON del SIC con UNGC.

El tramo de una planta sin contrato de venta se clasificaba ENTERO como
"comercializador" (UNGC) si algún SIC con UNGC de comprador lo tocaba aunque
fuera un día. Casos reales:

- GD La Hormiguita, abril 2026: su SIC UNGC empieza el 23 y el tramo salía
  UNGC del 1 al 30. Del 1 al 22 no tenía contrato: es bolsa libre.
- PSF Yurbaqua, febrero 2026: su SIC UNGC terminó el 12 y el tramo seguía
  UNGC hasta fin de mes. Del 13 en adelante es bolsa libre.

Ahora cada día cae en la piscina que le corresponde según la vigencia real del
SIC (fin efectivo: respeta terminaciones y relevos).
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


@pytest.fixture(autouse=True)
def _limpio():
    from apps.contratos.models import ContratoServicio
    from apps.mercado_xm.models import AsicSolicitud
    from apps.proyectos.models import Proyecto

    def vaciar():
        AsicSolicitud.objects.all().delete()
        ContratoServicio.objects.all().delete()
        Proyecto.objects.all().delete()

    # También al salir: la base en memoria se comparte entre módulos, y otro
    # (`test_proyectos_codigos_unicos`) cuenta todos los proyectos.
    vaciar()
    yield
    vaciar()


def _planta(nombre):
    """Una planta que representamos: entra a Cumplimiento por su contrato de
    representación vigente, no por la bandera del proyecto."""
    from apps.contratos.models import ContratoServicio
    from apps.proyectos.models import Proyecto

    planta = Proyecto.objects.create(
        nombre_comercial=nombre, estado="en_operacion",
        tipo_proyecto="minigranja", fecha_inicio_comercializacion=date(2025, 1, 1),
    )
    ContratoServicio.objects.create(proyecto=planta, servicio_aplica="representacion")
    return planta


def _gescon(**kw):
    from apps.mercado_xm.models import AsicSolicitud

    base = dict(
        estado_solicitud="publicado", codigo_sic_vendedor="UNGG",
        codigo_sic_comprador="UNGC", reemplaza_anterior=True,
        es_duplicado=False, uso_del_recurso=False,
    )
    base.update(kw)
    return AsicSolicitud.objects.create(**base)


def _tramos(out, planta):
    """{piscina: [(inicio, fin), ...]} de la planta en el remanente."""
    res: dict = {}
    for p in out["bolsa"]:
        if p["id"] == planta.id:
            res.setdefault(p["piscina"], []).append((p["segmento_inicio"], p["segmento_fin"]))
    return {k: sorted(v) for k, v in res.items()}


def test_sic_ungc_que_empieza_a_mitad_de_mes_no_cubre_los_dias_previos():
    from apps.mercado_xm.services.cumplimiento.piscinas import plantas_contratos

    hormiga = _planta("GD La Hormiguita")
    _gescon(tipo_solicitud="registro", codigo_sic_contrato="89610", proyecto=hormiga,
            contrato_interno="UNG-2026-Hormiga",
            fecha_inicio=date(2026, 4, 23), fecha_fin=date(2026, 12, 31))

    out = plantas_contratos(2026, 4, incluir_todos=True)

    assert _tramos(out, hormiga) == {
        "libre": [("2026-04-01", "2026-04-22")],
        "comercializador": [("2026-04-23", "2026-04-30")],
    }


def test_sic_ungc_terminado_a_mitad_de_mes_deja_libre_el_resto():
    from apps.mercado_xm.services.cumplimiento.piscinas import plantas_contratos

    yurbaqua = _planta("PSF - Yurbaqua")
    _gescon(tipo_solicitud="registro", codigo_sic_contrato="88787", proyecto=yurbaqua,
            contrato_interno="UNG-2026",
            fecha_inicio=date(2026, 1, 1), fecha_fin=date(2026, 5, 31))
    # La terminación se guarda SIN planta: el fin efectivo sale del SIC.
    _gescon(tipo_solicitud="terminacion", codigo_sic_contrato="88787", proyecto=None,
            fecha_inicio=None, fecha_fin=date(2026, 2, 12))

    out = plantas_contratos(2026, 2, incluir_todos=True)

    assert _tramos(out, yurbaqua) == {
        "comercializador": [("2026-02-01", "2026-02-12")],
        "libre": [("2026-02-13", "2026-02-28")],
    }
    ungc = next(p for p in out["bolsa_comercializador"] if p["id"] == yurbaqua.id)
    assert ungc["codigo_sic"] == "88787"
    assert ungc["fecha_fin"] == "2026-02-12", "el fin mostrado es el efectivo, no el registrado"


def test_sic_ungc_todo_el_mes_sigue_siendo_un_solo_tramo_ungc():
    from apps.mercado_xm.services.cumplimiento.piscinas import plantas_contratos

    sirius = _planta("GD Sirius")
    _gescon(tipo_solicitud="registro", codigo_sic_contrato="88805", proyecto=sirius,
            contrato_interno="UNG-2026",
            fecha_inicio=date(2026, 1, 15), fecha_fin=date(2026, 12, 31))

    out = plantas_contratos(2026, 3, incluir_todos=True)

    assert _tramos(out, sirius) == {"comercializador": [("2026-03-01", "2026-03-31")]}


def test_sin_sic_ungc_todo_el_tramo_es_libre():
    from apps.mercado_xm.services.cumplimiento.piscinas import plantas_contratos

    libre = _planta("Planta sin contrato")

    out = plantas_contratos(2026, 3, incluir_todos=True)

    assert _tramos(out, libre) == {"libre": [("2026-03-01", "2026-03-31")]}
