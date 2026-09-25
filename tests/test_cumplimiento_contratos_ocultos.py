"""Ocultar un contrato (por responsable) oculta también sus plantas.

Las vistas de /mem/cumplimiento esconden los contratos de un responsable marcado
`incluir_en_cumplimiento=False`. Pero la asignación planta→contrato se armaba
recorriendo SOLO los contratos visibles, así que una planta cuyo contrato estaba
oculto quedaba "sin contrato": en el simulador caía en la piscina "Sin contrato"
con 100% de despacho y el mes completo, y en /plantas-contratos salía como BOLSA
todo el mes. La planta sí tiene contrato; solo no se muestra.

Con "Ver ocultos" (`incluir_todos=True`) la planta vuelve a su contrato.
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
    - "Planta Visible" en un contrato sin responsable (siempre visible).
    - "Planta Oculta" en un contrato de un responsable NO relevante.
    """
    from apps.mercado_xm.models import AsicSolicitud
    from apps.ppa.models import PpaContrato, PpaResponsable
    from apps.proyectos.models import Proyecto

    AsicSolicitud.objects.all().delete()
    PpaContrato.objects.all().delete()
    PpaResponsable.objects.all().delete()
    Proyecto.objects.all().delete()

    def planta(nombre):
        return Proyecto.objects.create(
            nombre_comercial=nombre, estado="en_operacion", srv_representacion=True,
            tipo_proyecto="minigranja", fecha_inicio_comercializacion=date(2025, 1, 1),
        )

    visible, oculta = planta("Planta Visible"), planta("Planta Oculta")
    tercero = PpaResponsable.objects.create(nombre="Tercero", incluir_en_cumplimiento=False)

    def contrato(codigo, responsable=None):
        return PpaContrato.objects.create(
            numero_codigo_contrato=codigo, nombre_interno=codigo, tipo_contrato="venta",
            responsable=responsable, fecha_inicio=date(2026, 1, 1), fecha_fin=date(2030, 12, 31),
        )

    c_visible = contrato("VISIBLE-1")
    c_oculto = contrato("OCULTO-1", responsable=tercero)

    for codigo, sic, proy in (("VISIBLE-1", "1001", visible), ("OCULTO-1", "1002", oculta)):
        AsicSolicitud.objects.create(
            tipo_solicitud="registro", estado_solicitud="publicado",
            contrato_interno=codigo, codigo_sic_contrato=sic, proyecto=proy,
            codigo_sic_vendedor="UNGG", codigo_sic_comprador="BIAC",
            fecha_inicio=date(2026, 1, 1), fecha_fin=date(2030, 12, 31),
            porcentaje_despacho=0.5, reemplaza_anterior=True,
            es_duplicado=False, uso_del_recurso=False,
        )
    return {"visible": visible, "oculta": oculta, "c_visible": c_visible, "c_oculto": c_oculto}


def _ids_bolsa(out):
    return {p["id"] for p in out.get("bolsa", [])}


def test_piscinas_planta_de_contrato_oculto_no_cae_en_bolsa(datos):
    from apps.mercado_xm.services.cumplimiento.piscinas import plantas_contratos

    out = plantas_contratos(YEAR, MONTH, incluir_todos=False)

    nombres_venta = {c["nombre"] for c in out["venta"]}
    assert "OCULTO-1" not in nombres_venta, "el contrato oculto no se muestra"
    assert "VISIBLE-1" in nombres_venta
    assert datos["oculta"].id not in _ids_bolsa(out), (
        "la planta de un contrato oculto NO es bolsa: tiene contrato, solo no se ve")
    assert datos["visible"].id not in _ids_bolsa(out)


def test_piscinas_con_ver_ocultos_la_planta_vuelve_a_su_contrato(datos):
    from apps.mercado_xm.services.cumplimiento.piscinas import plantas_contratos

    out = plantas_contratos(YEAR, MONTH, incluir_todos=True)

    oculto = next(c for c in out["venta"] if c["nombre"] == "OCULTO-1")
    assert [p["id"] for p in oculto["plantas"]] == [datos["oculta"].id]
    assert datos["oculta"].id not in _ids_bolsa(out)


def _simulador(monkeypatch, incluir_todos):
    from apps.mercado_xm.services.cumplimiento import simulador as mod

    def _sin_api():
        raise RuntimeError("sin API de generación en tests")

    monkeypatch.setattr(mod, "_unergy_token", _sin_api)
    return mod.simulador(YEAR, MONTH, incluir_todos=incluir_todos)


def test_simulador_planta_de_contrato_oculto_no_queda_sin_contrato(datos, monkeypatch):
    out = _simulador(monkeypatch, incluir_todos=False)

    por_id = {p["id"]: p for p in out["plantas"]}
    assert datos["oculta"].id not in por_id, (
        "la planta de un contrato oculto no debe aparecer suelta en 'Sin contrato'")
    assert por_id[datos["visible"].id]["contrato_id"] == datos["c_visible"].id
    assert {c["id"] for c in out["contratos"]} == {datos["c_visible"].id}


def test_simulador_con_ver_ocultos_la_planta_vuelve_a_su_contrato(datos, monkeypatch):
    out = _simulador(monkeypatch, incluir_todos=True)

    por_id = {p["id"]: p for p in out["plantas"]}
    fila = por_id[datos["oculta"].id]
    assert fila["contrato_id"] == datos["c_oculto"].id
    assert fila["pct_despacho"] == 0.5, "con su contrato, el despacho es el real, no 100%"
