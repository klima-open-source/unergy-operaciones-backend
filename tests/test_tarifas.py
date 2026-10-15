"""Tarifas por servicio y el cargo de administración (rama de servicios y tarifas, paso 2).

Esquema de Sara (2026-10-14): la base y el índice en el servicio; `tarifas`, el valor
de cada periodo (año, o año y mes en los PPA). Los atajos de siempre del contrato
(`tarifa_cgm`, `indexacion_representacion`, `tarifa_base`…) se leen y se escriben
sobre ellos (`apps/contratos/services/tarifas.py`), y los precios mes a mes de los
PPA, que vivían en `ppa_tarifas`, también. La administración es un cargo adicional:
un servicio con su tarifa en %, que no cuenta para qué servicios tiene la planta
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


def _ppa(**kw):
    from apps.ppa.models import PpaContrato

    kw.setdefault("tipo_contrato", "venta")
    return PpaContrato.objects.create(**kw)


def _servicio(contrato, servicio):
    """(base, índice, [(año, mes, valor)…]) del servicio, o None si no existe."""
    from apps.contratos.models import Servicio

    s = Servicio.objects.filter(contrato=contrato, servicio=servicio).first()
    if s is None:
        return None
    return (s.tarifa_base, s.tipo_indexacion,
            [(t.anio, t.mes, t.valor) for t in s.tarifas.order_by("anio", "mes")])


# ── Los atajos de siempre, sobre el servicio y `tarifas` ──────────────────────

def test_el_historial_de_representacion_entra_y_sale_con_la_misma_forma(datos):
    historial = [
        {"año": 2024, "ipc": None, "valor": 6.0, "esBase": True},
        {"año": 2025, "ipc": 5.2, "valor": 6.312},
    ]
    contrato = _contrato(proyecto=datos["planta"], indexacion_representacion=historial,
                         tarifa_representacion=Decimal("6"))
    contrato.refresh_from_db()

    assert _servicio(contrato, "representacion") == (
        Decimal("6"), "ipc", [(2024, None, Decimal("6")), (2025, None, Decimal("6.312"))])
    # El índice aplicado ya no se guarda: sale de comparar 6,312 con 6.
    assert contrato.indexacion_representacion == historial
    assert contrato.tarifa_representacion == Decimal("6")


def test_sin_historial_hay_base_y_no_hay_anios(datos):
    contrato = _contrato(proyecto=datos["planta"], tarifa_representacion=Decimal("5"))
    assert _servicio(contrato, "representacion") == (Decimal("5"), "ipc", [])
    # Sin años no hay historial que mostrar: como cuando el JSON estaba vacío.
    assert contrato.indexacion_representacion is None


def test_el_indice_de_texto_libre_se_reconoce():
    from apps.contratos.services import tarifas

    ipp = SimpleNamespace(indice_indexacion="IPP serie oferta interna (marzo 2025)")
    assert tarifas.tipo_indexacion_de(ipp, "representacion") == "ipp"
    # Lo que no se indexa queda sin tipo: solo existen IPC e IPP.
    assert tarifas.tipo_indexacion_de(ipp, "administracion") is None
    assert tarifas.tipo_indexacion_de(SimpleNamespace(indice_indexacion="Fijo"), "venta") is None


def test_cambiar_el_indice_del_contrato_cambia_el_de_sus_servicios(datos):
    contrato = _contrato(proyecto=datos["planta"], tarifa_representacion=Decimal("5"))
    contrato.indice_indexacion = "IPP"
    contrato.save()
    assert _servicio(contrato, "representacion")[1] == "ipp"


def test_mantenimiento_guarda_el_anual_y_da_el_mensual(datos):
    contrato = _contrato(servicio_aplica="mantenimiento", proyecto=datos["planta"],
                         fecha_firma_contrato=date(2023, 1, 1), tarifa_anual=Decimal("48000000"),
                         indexacion_anual=[{"anio": 2023, "valor": 48000000, "ipc_aplicado": None},
                                           {"anio": 2024, "valor": 52454400, "ipc_aplicado": 9.28}])
    contrato.refresh_from_db()

    assert contrato.tarifa_anual == Decimal("48000000")
    assert contrato.tarifa_base == Decimal("48000000")  # el mismo dato, nombre del PPA
    assert contrato.tarifa_mensual == Decimal("4371200.00")  # el año vigente / 12
    assert [f["valor"] for f in contrato.indexacion_mensual] == [4000000.0, 4371200.0]
    assert contrato.indexacion_anual[1]["ipc_aplicado"] == pytest.approx(9.28)


def test_la_tarifa_de_cgm_espera_a_que_exista_su_servicio(datos):
    """La API registra la lista de servicios DESPUÉS del `save()`: la tarifa de CGM
    queda pendiente y entra cuando el servicio existe."""
    from apps.contratos.services import servicios, tarifas

    contrato = _contrato(proyecto=datos["planta"], tarifa_representacion=Decimal("6"),
                         tarifa_cgm=Decimal("5"))
    assert _servicio(contrato, "cgm") is None
    servicios.registrar(contrato, ["representacion", "cgm"])
    tarifas.aplicar(contrato)

    assert _servicio(contrato, "cgm") == (Decimal("5"), "ipc", [])


# ── El cargo de administración ────────────────────────────────────────────────

def test_guardar_deriva_el_cargo_de_administracion(datos):
    from apps.contratos.services import grupos

    contrato = _contrato(proyecto=datos["planta"], tarifa_representacion=Decimal("6"),
                         tarifa_admin=Decimal("0.038"))

    assert _servicio(contrato, "administracion") == (Decimal("0.038"), None, [])
    assert contrato.tarifa_admin == Decimal("0.038")
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


def test_dos_inversionistas_de_la_misma_planta_no_se_cobran_dos_veces(datos):
    """Minigranja: un contrato de representación por inversionista, cada uno con su
    administración sobre su parte. No es un cobro doble."""
    from apps.clientes.models import Cliente
    from apps.contratos.services import tarifas
    from apps.proyectos.models import ProyectoInversionista

    bia = Cliente.objects.create(razon_social_nombre="Bia")
    suno = Cliente.objects.create(razon_social_nombre="Suno")
    for cliente in (bia, suno):
        ProyectoInversionista.objects.create(proyecto=datos["planta"], cliente=cliente)
    _contrato(proyecto=datos["planta"], inversionista=bia, estado="firmado",
              tarifa_admin=Decimal("0.038"))
    de_suno = _contrato(proyecto=datos["planta"], inversionista=suno, estado="firmado",
                        tarifa_admin=Decimal("0.038"))

    assert tarifas.avisos_de_administracion(de_suno) == []


# ── PPA: la base y los precios del mes, en el servicio de compra o venta ──────

def test_la_base_del_ppa_se_lee_y_se_escribe_en_su_servicio(datos):
    contrato = _ppa(tarifa_base=Decimal("308"), indice_indexacion="IPP")
    contrato.refresh_from_db()
    assert contrato.tarifa_base == Decimal("308")
    assert _servicio(contrato, "venta") == (Decimal("308"), "ipp", [])

    contrato.tarifa_base = Decimal("310")
    contrato.save()
    assert _servicio(contrato, "venta")[0] == Decimal("310")


def test_los_precios_del_mes_reemplazan_la_tabla_entera(datos):
    from apps.contratos.services import tarifas

    contrato = _ppa(tipo_contrato="compra")
    tarifas.poner_filas_ppa(contrato, [
        {"año": 2025, "mes": 1, "tarifa": 300},
        {"año": 2025, "mes": 2, "tarifa": None},  # sin precio: no se guarda
    ])
    tarifas.poner_filas_ppa(contrato, [{"año": 2025, "mes": 3, "tarifa": 302.5}])

    assert _servicio(contrato, "compra")[2] == [(2025, 3, Decimal("302.5"))]
    assert tarifas.precios_ppa(2025, 3) == {contrato.id: Decimal("302.5")}
    assert tarifas.precios_ppa(2025, 1) == {}


def test_los_precios_del_mes_no_tocan_los_anios_de_otro_servicio(datos):
    """Las filas por año (sin mes) de representación no se mezclan con las del PPA."""
    from apps.contratos.services import tarifas

    rep = _contrato(proyecto=datos["planta"], tarifa_representacion=Decimal("6"),
                    indexacion_representacion=[{"año": 2025, "valor": 6}])
    contrato = _ppa()
    tarifas.poner_filas_ppa(contrato, [{"año": 2025, "mes": 1, "tarifa": 300}])

    assert tarifas.precios_ppa(2025, 1) == {contrato.id: Decimal("300")}
    assert _servicio(rep, "representacion")[2] == [(2025, None, Decimal("6"))]
