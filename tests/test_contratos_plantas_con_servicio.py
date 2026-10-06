"""Qué plantas tienen un servicio hoy (`apps/contratos/services/plantas.py`).

La regla que reemplaza las banderas `srv_*`: contrato vigente con ese servicio en
`servicios`, sin mirar el estado del proyecto ni la bandera; y a una planta en
comunidad energética (marca del PPA, que llega por sus vínculos) no se le presta
representación ni CGM.
"""
from datetime import date, timedelta

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

HOY = date(2026, 10, 6)


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
def _transaccion():
    from django.db import transaction

    atomica = transaction.atomic()
    atomica.__enter__()
    yield
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _proyecto(nombre, **kw):
    from apps.proyectos.models import Proyecto

    return Proyecto.objects.create(nombre_comercial=nombre, **kw)


def _servicio(proyecto, servicio_aplica, servicios=None, **kw):
    from apps.contratos.models import ContratoServicio
    from apps.contratos.services import servicios as servicios_service

    contrato = ContratoServicio.objects.create(
        proyecto=proyecto, servicio_aplica=servicio_aplica, **kw)
    if servicios is not None:
        servicios_service.registrar(contrato, servicios)
    return contrato


def _ppa(*proyectos, **kw):
    from apps.ppa.models import PpaContrato, PpaContratoProyecto

    kw.setdefault("tipo_contrato", "venta")
    contrato = PpaContrato.objects.create(**kw)
    for p in proyectos:
        PpaContratoProyecto.objects.create(contrato=contrato, proyecto=p)
    return contrato


def _con(servicio):
    from apps.contratos.services import plantas
    from apps.proyectos.models import Proyecto

    return set(Proyecto.objects.filter(plantas.filtro_proyectos_con(servicio, HOY))
               .values_list("nombre_comercial", flat=True))


def _por_proyecto():
    from apps.contratos.services import plantas
    from apps.proyectos.models import Proyecto

    nombres = dict(Proyecto.objects.values_list("id", "nombre_comercial"))
    return {nombres[pid]: subs for pid, subs in plantas.servicios_por_proyecto(HOY).items()}


# ── La regla ─────────────────────────────────────────────────────────────


def test_operacion_es_mantenimiento_arriendo_o_internet():
    for nombre, servicio in (("M", "mantenimiento"), ("A", "arriendo"), ("I", "internet")):
        _servicio(_proyecto(nombre), servicio)
    _servicio(_proyecto("R"), "representacion")

    assert _con("operacion") == {"M", "A", "I"}
    assert _con("arriendo") == {"A"}


def test_no_mira_el_estado_del_proyecto_ni_la_bandera():
    """Una planta en construcción con O&M firmado tiene operación; una con la
    bandera encendida y sin contrato, no."""
    _servicio(_proyecto("En construcción", estado="en_construccion"), "mantenimiento")
    _proyecto("Solo bandera", estado="en_operacion", srv_operacion=True)

    assert _con("operacion") == {"En construcción"}


def test_un_contrato_que_ya_no_rige_no_cuenta():
    _servicio(_proyecto("Terminado"), "mantenimiento", estado="terminado")
    _servicio(_proyecto("Vencido"), "mantenimiento", fecha_fin=HOY - timedelta(days=1))
    _servicio(_proyecto("Vence hoy"), "mantenimiento", fecha_fin=HOY)
    _servicio(_proyecto("En renovación"), "mantenimiento", estado="en_renovacion")

    assert _con("operacion") == {"Vence hoy", "En renovación"}


def test_cgm_sale_de_los_servicios_registrados_no_de_la_tarifa():
    _servicio(_proyecto("Las dos"), "representacion", ["representacion", "cgm"])
    _servicio(_proyecto("Solo CGM"), "representacion", ["cgm"])
    _servicio(_proyecto("Solo rep. con tarifa CGM"), "representacion", tarifa_cgm=5)

    assert _con("cgm") == {"Las dos", "Solo CGM"}
    assert _con("representacion") == {"Las dos", "Solo rep. con tarifa CGM"}
    assert _con("representacion_cgm") == {"Las dos", "Solo CGM", "Solo rep. con tarifa CGM"}


def test_un_ppa_llega_a_todas_sus_plantas_y_vence_por_fecha():
    a, b = _proyecto("A"), _proyecto("B")
    _ppa(a, b, tipo_contrato="venta")
    _ppa(_proyecto("Compra"), tipo_contrato="compra")
    _ppa(_proyecto("PPA vencido"), fecha_fin=HOY - timedelta(days=1))

    assert _con("ppa") == {"A", "B", "Compra"}
    assert _con("venta") == {"A", "B"}


def test_un_nombre_que_no_es_servicio_es_un_error():
    from apps.contratos.services import plantas

    with pytest.raises(ValueError):
        plantas.subservicios_de_nombre("operacion_y_algo")


# ── Comunidades ──────────────────────────────────────────────────────────


def test_en_comunidad_pierde_representacion_y_cgm_pero_no_operacion():
    comunidad = _proyecto("Comunidad")
    _servicio(comunidad, "representacion", ["representacion", "cgm"])
    _servicio(comunidad, "mantenimiento")
    _ppa(comunidad, es_comunidad_energetica=True,
         fecha_entrada_comunidad=HOY - timedelta(days=30))
    _servicio(_proyecto("Normal"), "representacion")

    assert _con("representacion") == {"Normal"}
    assert _con("cgm") == set()
    assert _con("operacion") == {"Comunidad"}
    assert _con("ppa") == {"Comunidad"}
    assert _por_proyecto()["Comunidad"] == {"mantenimiento", "venta"}


def test_la_comunidad_no_excluye_antes_de_su_fecha_de_entrada():
    futura = _proyecto("Entra en 2027")
    _servicio(futura, "representacion")
    _ppa(futura, es_comunidad_energetica=True, fecha_entrada_comunidad=date(2027, 1, 1))

    assert _con("representacion") == {"Entra en 2027"}


def test_un_ppa_de_comunidad_vencido_ya_no_excluye():
    salio = _proyecto("Salió")
    _servicio(salio, "representacion")
    _ppa(salio, es_comunidad_energetica=True, fecha_entrada_comunidad=date(2025, 1, 1),
         fecha_fin=HOY - timedelta(days=1))

    assert _con("representacion") == {"Salió"}


# ── Las dos formas dicen lo mismo ────────────────────────────────────────


def test_servicios_por_proyecto_coincide_con_el_filtro():
    from apps.contratos.services import grupos

    p = _proyecto("Completa")
    _servicio(p, "representacion", ["representacion", "cgm"])
    _servicio(p, "arriendo")
    _ppa(p, tipo_contrato="compra")
    _servicio(_proyecto("Vencida"), "internet", fecha_fin=HOY - timedelta(days=1))
    _proyecto("Sin nada")

    por_proyecto = _por_proyecto()
    assert por_proyecto == {"Completa": {"representacion", "cgm", "arriendo", "compra"}}
    for sub in grupos.GRUPO_DE_SUBSERVICIO:
        assert _con(sub) == {n for n, subs in por_proyecto.items() if sub in subs}


# ── Paso 2: los que leían `srv_operacion` ────────────────────────────────


def test_se_monitorean_las_que_generan_y_operamos():
    """`en_operacion` dice que genera; el contrato, que la operamos. Un arriendo
    firmado en desarrollo no la mete al sondeo, y la bandera sola ya no basta."""
    from apps.contratos.services import plantas
    from apps.proyectos.models import Proyecto

    _servicio(_proyecto("Opera", estado="en_operacion"), "mantenimiento")
    _servicio(_proyecto("En desarrollo", estado="en_desarrollo"), "arriendo")
    _proyecto("Solo bandera", estado="en_operacion", srv_operacion=True)

    assert set(Proyecto.objects.filter(plantas.filtro_operadas(HOY))
               .values_list("nombre_comercial", flat=True)) == {"Opera"}


def test_reconectadores_e_informe_de_puesta_en_marcha_usan_el_contrato():
    from api.v1.informe_om.queryset import proyectos_con_informe
    from api.v1.reconectadores.queryset import proyectos_con_relay

    con_sv = _proyecto("Con SolarView", estado="en_operacion", tipo_proyecto="minigranja",
                       project_id_solarview="sv-1")
    _servicio(con_sv, "internet")
    _servicio(_proyecto("Sin SolarView", estado="en_operacion",
                        tipo_proyecto="minigranja"), "mantenimiento")
    _proyecto("Solo bandera", estado="en_operacion", tipo_proyecto="minigranja",
              project_id_solarview="sv-2", srv_operacion=True)

    assert {p.nombre_comercial for p in proyectos_con_relay()} == {"Con SolarView"}
    assert {p.nombre_comercial for p in proyectos_con_informe()} == {
        "Con SolarView", "Sin SolarView"}


def test_portafolios_cuenta_en_operacion_o_con_contrato():
    from apps.proyectos.services import portafolios

    _proyecto("En operación sin contrato", estado="en_operacion")
    _servicio(_proyecto("En desarrollo con O&M", estado="en_desarrollo"), "mantenimiento")
    _proyecto("Ninguna", estado="en_desarrollo", srv_operacion=True)

    from apps.proyectos.models import Proyecto

    nombres = dict(Proyecto.objects.values_list("id", "nombre_comercial"))
    assert {nombres[i] for i in portafolios.ids_operativos()} == {
        "En operación sin contrato", "En desarrollo con O&M"}


def test_nadie_nuevo_lee_srv_operacion():
    """Los que quedan son los pasos siguientes (API de proyectos, pipeline, el
    modelo) y el informe que mide el barrido. La lista solo puede achicarse."""
    from pathlib import Path

    raiz = Path(__file__).resolve().parents[1]
    permitidos = {
        "apps/proyectos/models.py",
        "api/v1/proyectos/serializers.py",
        "apps/comercial/services/pipeline.py",
        "apps/contratos/management/commands/revisar_servicios_banderas.py",
    }
    leen = {
        str(p.relative_to(raiz)).replace("\\", "/")
        for carpeta in ("apps", "api", "config")
        for p in (raiz / carpeta).rglob("*.py")
        if "migrations" not in p.parts and "srv_operacion" in p.read_text(encoding="utf-8")
    }
    assert leen <= permitidos, sorted(leen - permitidos)
