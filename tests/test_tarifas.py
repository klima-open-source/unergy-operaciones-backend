"""Tarifas por servicio y el cargo de administración (rama de servicios y tarifas, paso 1).

Esquema de Sara (2026-10-14): la base y el índice en el servicio; `tarifas`, el valor
de cada periodo (año, o año y mes en los PPA). Mientras convivan con las columnas de
`contratos` y con `ppa_tarifas`, `sincronizar` los deriva de ellas
(`apps/contratos/services/tarifas.py`). La administración es un cargo adicional: un
servicio con su tarifa en %, que no cuenta para qué servicios tiene la planta
(decisión de Sara, 2026-10-13).
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

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

    from apps.proyectos.models import Proyecto

    atomica = transaction.atomic()
    atomica.__enter__()
    yield {"planta": Proyecto.objects.create(nombre_comercial="Planta QA")}
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _contrato(**kw):
    from apps.contratos.models import ContratoServicio

    kw.setdefault("servicio_aplica", "representacion")
    return ContratoServicio.objects.create(**kw)


def _servicio(contrato, servicio):
    """(base, índice, [(año, mes, valor)…]) del servicio, o None si no existe."""
    from apps.contratos.models import Servicio

    s = Servicio.objects.filter(contrato=contrato, servicio=servicio).first()
    if s is None:
        return None
    return (s.tarifa_base, s.tipo_indexacion,
            [(t.anio, t.mes, t.valor) for t in s.tarifas.order_by("anio", "mes")])


# ── Del historial a las filas (puro) ──────────────────────────────────────────

def test_el_historial_da_una_fila_por_anio_sin_mes():
    from apps.contratos.services import tarifas

    filas = tarifas.filas_desde_historial([
        {"año": 2024, "ipc": None, "valor": 6.0, "esBase": True},
        {"año": 2025, "ipc": 5.2, "valor": 6.312},
        {"año": None, "valor": 1},  # sin año: no es un periodo
    ])
    assert [(f["anio"], f["mes"], f["valor"]) for f in filas] == [
        (2024, None, Decimal("6.0")), (2025, None, Decimal("6.312"))]


def test_el_indice_de_texto_libre_se_reconoce():
    from apps.contratos.services import tarifas

    ipp = SimpleNamespace(indice_indexacion="IPP serie oferta interna (marzo 2025)")
    assert tarifas.tipo_indexacion_de(ipp, "representacion") == "ipp"
    assert tarifas.tipo_indexacion_de(SimpleNamespace(indice_indexacion=None), "cgm") == "ipc"
    # Lo que no se indexa queda sin tipo: solo existen IPC e IPP.
    assert tarifas.tipo_indexacion_de(ipp, "administracion") is None
    assert tarifas.tipo_indexacion_de(SimpleNamespace(indice_indexacion="Fijo"), "venta") is None


# ── Al guardar un contrato de servicio ────────────────────────────────────────

def test_guardar_pone_la_base_en_el_servicio_y_el_historial_en_tarifas(datos):
    contrato = _contrato(
        proyecto=datos["planta"], tarifa_representacion=Decimal("6"),
        indexacion_representacion=[{"año": 2024, "valor": 6, "esBase": True},
                                   {"año": 2025, "ipc": 5.2, "valor": 6.312}],
    )
    assert _servicio(contrato, "representacion") == (
        Decimal("6"), "ipc", [(2024, None, Decimal("6")), (2025, None, Decimal("6.312"))])


def test_sin_historial_queda_solo_la_base_sin_inventar_anio(datos):
    contrato = _contrato(proyecto=datos["planta"], tarifa_representacion=Decimal("5"),
                         fecha_firma_contrato=date(2023, 4, 1))
    assert _servicio(contrato, "representacion") == (Decimal("5"), "ipc", [])


def test_mantenimiento_toma_su_base_anual_de_tarifa_base(datos):
    contrato = _contrato(servicio_aplica="mantenimiento", proyecto=datos["planta"],
                         tarifa_base=Decimal("12000000"))
    assert _servicio(contrato, "mantenimiento") == (Decimal("12000000"), "ipc", [])


def test_guardar_deriva_el_cargo_de_administracion(datos):
    from apps.contratos.services import grupos

    contrato = _contrato(proyecto=datos["planta"], tarifa_representacion=Decimal("6"),
                         tarifa_admin=Decimal("0.038"))

    assert _servicio(contrato, "administracion") == (Decimal("0.038"), None, [])
    # Cargo adicional: no es un subservicio del contrato.
    assert grupos.subservicios_de(contrato) == ["representacion"]


def test_quitar_la_tarifa_de_administracion_quita_el_cargo(datos):
    from apps.contratos.models import Servicio

    contrato = _contrato(proyecto=datos["planta"], tarifa_admin=Decimal("0.038"))
    contrato.tarifa_admin = None
    contrato.save()

    assert not Servicio.objects.filter(contrato=contrato, servicio="administracion").exists()


def test_un_om_puede_llevar_administracion_y_un_arriendo_no(datos):
    om = _contrato(servicio_aplica="mantenimiento", proyecto=datos["planta"],
                   tarifa_admin=Decimal("0.05"))
    arriendo = _contrato(servicio_aplica="arriendo", proyecto=datos["planta"],
                         tarifa_admin=Decimal("0.05"))

    assert _servicio(om, "administracion") is not None
    assert _servicio(arriendo, "administracion") is None


def test_la_administracion_no_cuenta_para_los_servicios_de_la_planta(datos):
    from apps.contratos.services import plantas
    from apps.plataforma.services.fechas import hoy_col

    _contrato(servicio_aplica="mantenimiento", proyecto=datos["planta"], estado="firmado",
              tarifa_admin=Decimal("0.05"))
    servicios = plantas.servicios_por_proyecto(hoy_col()).get(datos["planta"].id, set())

    assert "administracion" not in servicios
    assert "mantenimiento" in servicios


def test_administracion_en_dos_contratos_de_la_planta_avisa(datos):
    from apps.contratos.services import tarifas

    _contrato(proyecto=datos["planta"], estado="firmado", tarifa_admin=Decimal("0.038"))
    om = _contrato(servicio_aplica="mantenimiento", proyecto=datos["planta"], estado="firmado",
                   tarifa_admin=Decimal("0.05"))

    avisos = tarifas.avisos_de_administracion(om)
    assert len(avisos) == 1 and "administración" in avisos[0]


# ── PPA: `tarifa_base` y `ppa_tarifas` van al servicio de compra o venta ──────

def _ppa(**kw):
    from apps.ppa.models import PpaContrato

    kw.setdefault("tipo_contrato", "venta")
    return PpaContrato.objects.create(**kw)


def test_un_ppa_con_solo_base_queda_con_la_base_y_sin_filas(datos):
    contrato = _ppa(tarifa_base=Decimal("308"),
                    indice_indexacion="IPP serie oferta interna (marzo 2025)")
    assert _servicio(contrato, "venta") == (Decimal("308"), "ipp", [])


def test_los_precios_mensuales_del_ppa_pasan_con_su_mes(datos):
    from apps.contratos.services import tarifas
    from apps.ppa.models import PpaTarifa

    contrato = _ppa(tipo_contrato="compra")
    PpaTarifa.objects.bulk_create([
        PpaTarifa(contrato=contrato, año=2025, mes=1, tarifa=Decimal("300")),
        PpaTarifa(contrato=contrato, año=2025, mes=2, tarifa=Decimal("302.5")),
        PpaTarifa(contrato=contrato, año=2025, mes=3, tarifa=None),  # sin precio: no pasa
    ])
    assert tarifas.sincronizar(contrato) is True
    assert _servicio(contrato, "compra") == (
        None, "ipc", [(2025, 1, Decimal("300")), (2025, 2, Decimal("302.5"))])
    assert tarifas.sincronizar(contrato) is False  # idempotente


def test_crear_un_ppa_con_tarifas_las_copia(datos):
    from apps.ppa.services import escritura

    resultado = escritura.crear_ppa(
        tipo_contrato="venta", datos={"indice_indexacion": "Fijo"}, proyecto_ids=[],
        tarifas=[{"año": 2026, "mes": 5, "tarifa": Decimal("410")}],
    )
    assert _servicio(resultado.contrato, "venta") == (None, None, [(2026, 5, Decimal("410"))])


def test_registrar_tarifas_solo_lee_sin_aplicar(datos):
    from django.core.management import call_command

    from apps.contratos.models import Servicio, Tarifa

    contrato = _contrato(proyecto=datos["planta"], tarifa_representacion=Decimal("6"),
                         indexacion_representacion=[{"año": 2024, "valor": 6}])
    # Como un contrato de antes del paso 1.
    Tarifa.objects.all().delete()
    Servicio.objects.filter(contrato=contrato).update(tarifa_base=None, tipo_indexacion=None)

    call_command("registrar_tarifas")
    assert not Tarifa.objects.exists()
    call_command("registrar_tarifas", aplicar=True)
    assert _servicio(contrato, "representacion") == (
        Decimal("6"), "ipc", [(2024, None, Decimal("6"))])
