"""Cumplimiento muestra todos los contratos PPA, también los que gestiona un tercero.

Hasta el 2026-10-14 el catálogo `ppa_responsables` escondía los contratos
"externos" (BIA, Sol&Cielo…) de las vistas de /mem/cumplimiento. Sara decidió
quitar ese filtro sin reemplazarlo por una bandera: ahora todos se ven, cada planta
con su contrato y su % de despacho real, y ninguna cae en bolsa ni en "Sin
contrato".
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


YEAR, MONTH = 2026, 7


@pytest.fixture
def datos():
    """Dos plantas en operación, cada una en su contrato de venta:
    - "Planta Propia" en un contrato de Unergy.
    - "Planta Externa" en un contrato que gestiona un tercero (antes "Externo").
    """
    from apps.contratos.models import ContratoServicio
    from apps.mercado_xm.models import AsicSolicitud
    from apps.ppa.models import PpaContrato
    from apps.proyectos.models import Proyecto

    AsicSolicitud.objects.all().delete()
    PpaContrato.objects.all().delete()
    ContratoServicio.objects.all().delete()
    Proyecto.objects.all().delete()

    def planta(nombre):
        # Entra a Cumplimiento por su contrato de representación vigente.
        p = Proyecto.objects.create(
            nombre_comercial=nombre, estado="en_operacion",
            tipo_proyecto="minigranja", fecha_inicio_comercializacion=date(2025, 1, 1),
        )
        ContratoServicio.objects.create(proyecto=p, servicio_aplica="representacion")
        return p

    propia, externa = planta("Planta Propia"), planta("Planta Externa")

    def contrato(codigo):
        return PpaContrato.objects.create(
            numero_codigo_contrato=codigo, nombre_interno=codigo, tipo_contrato="venta",
            fecha_inicio=date(2026, 1, 1), fecha_fin=date(2030, 12, 31),
        )

    c_propio = contrato("PROPIO-1")
    c_externo = contrato("EXTERNO-1")

    for codigo, sic, proy in (("PROPIO-1", "1001", propia), ("EXTERNO-1", "1002", externa)):
        AsicSolicitud.objects.create(
            tipo_solicitud="registro", estado_solicitud="publicado",
            contrato_interno=codigo, codigo_sic_contrato=sic, proyecto=proy,
            codigo_sic_vendedor="UNGG", codigo_sic_comprador="BIAC",
            fecha_inicio=date(2026, 1, 1), fecha_fin=date(2030, 12, 31),
            porcentaje_despacho=0.5, reemplaza_anterior=True,
            es_duplicado=False, uso_del_recurso=False,
        )
    yield {"propia": propia, "externa": externa, "c_propio": c_propio, "c_externo": c_externo}

    # Lo creado no se queda: la base en memoria se comparte entre módulos, y otro
    # (`test_proyectos_codigos_unicos`) cuenta todos los proyectos.
    AsicSolicitud.objects.all().delete()
    PpaContrato.objects.all().delete()
    ContratoServicio.objects.all().delete()
    Proyecto.objects.all().delete()


def _ids_bolsa(out):
    return {p["id"] for p in out.get("bolsa", [])}


def test_piscinas_muestra_los_dos_contratos_con_su_planta(datos):
    from apps.mercado_xm.services.cumplimiento.piscinas import plantas_contratos

    out = plantas_contratos(YEAR, MONTH)

    por_nombre = {c["nombre"]: c for c in out["venta"]}
    assert [p["id"] for p in por_nombre["PROPIO-1"]["plantas"]] == [datos["propia"].id]
    assert [p["id"] for p in por_nombre["EXTERNO-1"]["plantas"]] == [datos["externa"].id]
    assert not {datos["propia"].id, datos["externa"].id} & _ids_bolsa(out)
    assert "responsable" not in por_nombre["EXTERNO-1"]


def _simulador(monkeypatch):
    from apps.mercado_xm.services.cumplimiento import simulador as mod

    def _sin_api():
        raise RuntimeError("sin API de generación en tests")

    monkeypatch.setattr(mod, "_unergy_token", _sin_api)
    return mod.simulador(YEAR, MONTH)


def test_simulador_cada_planta_queda_en_su_contrato(datos, monkeypatch):
    out = _simulador(monkeypatch)

    por_id = {p["id"]: p for p in out["plantas"]}
    assert por_id[datos["propia"].id]["contrato_id"] == datos["c_propio"].id
    fila = por_id[datos["externa"].id]
    assert fila["contrato_id"] == datos["c_externo"].id
    assert fila["pct_despacho"] == 0.5, "con su contrato, el despacho es el real, no 100%"
    assert {c["id"] for c in out["contratos"]} == {datos["c_propio"].id, datos["c_externo"].id}
