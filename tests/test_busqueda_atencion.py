"""Búsqueda global (⌘K) y «lo que pide atención» del Dashboard (fase 5 de UX)."""
from datetime import timedelta

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
    from apps.arriendos.models import ArrArrendador
    from apps.clientes.models import Cliente
    from apps.contratos.models import Contrato
    from apps.fronteras.models import Frontera
    from apps.proyectos.models import Proyecto

    def borrar():
        ArrArrendador.objects.all().delete()
        Frontera.objects.all().delete()
        Contrato.objects.all().delete()
        Proyecto.objects.all().delete()
        Cliente.objects.all().delete()

    borrar()
    yield
    borrar()


def _hoy():
    from apps.plataforma.services.fechas import hoy_col

    return hoy_col()


def test_busca_en_los_cuatro_tipos_con_la_ruta_de_su_ficha():
    from apps.clientes.models import Cliente
    from apps.contratos.models import ContratoServicio
    from apps.fronteras.models import Frontera
    from apps.plataforma.services.busqueda import buscar
    from apps.proyectos.models import Proyecto

    p = Proyecto.objects.create(nombre_comercial="MGS 0012 La Reserva", codigo_tsf="COLSANT9P1")
    Proyecto.objects.create(nombre_comercial="Otra planta")
    cl = Cliente.objects.create(razon_social_nombre="Reserva Energía S.A.S.", nit_cedula="900111")
    c = ContratoServicio.objects.create(servicio_aplica="arriendo", proyecto=p)
    Frontera.objects.create(codigo_frontera="frt69663", nombre_frontera="Reserva", tipo_frontera="generacion", proyecto=p)

    r = buscar("reserva")
    assert [x["url"] for x in r["proyectos"]] == [f"/proyectos/{p.id}"]
    assert [x["url"] for x in r["clientes"]] == [f"/clientes/{cl.id}"]
    assert r["contratos"][0] == {**r["contratos"][0], "url": f"/contratos/{c.id}", "titulo": "Arriendo · MGS 0012 La Reserva"}
    assert r["fronteras"][0]["url"] == f"/proyectos/{p.id}?tab=info&seccion=fronteras"
    assert buscar("COLSANT9")["proyectos"][0]["id"] == p.id
    assert buscar("900111")["clientes"][0]["id"] == cl.id


def test_un_ppa_lleva_su_tipo_en_la_ruta():
    """`?tipo=ppa`: la ficha de contrato va directo al PPA, sin probar antes el
    endpoint de servicio (que para un PPA responde 404)."""
    from apps.contratos.models import Contrato
    from apps.plataforma.services.busqueda import buscar

    c = Contrato.objects.create(grupo="ppa", nombre_interno="PPA Reserva Norte")

    assert buscar("reserva norte")["contratos"][0]["url"] == f"/contratos/{c.id}?tipo=ppa"


def test_una_letra_no_busca():
    from apps.plataforma.services.busqueda import buscar

    assert buscar(" r ") == {"proyectos": [], "clientes": [], "contratos": [], "fronteras": []}


def test_atencion_contratos_por_vencer_solo_los_vivos_de_los_proximos_60_dias():
    from apps.contratos.models import ContratoServicio
    from apps.plataforma.services.atencion import atencion
    from apps.proyectos.models import Proyecto

    hoy = _hoy()
    p = Proyecto.objects.create(nombre_comercial="Planta A")
    pronto = ContratoServicio.objects.create(servicio_aplica="mantenimiento", proyecto=p, estado="firmado", fecha_fin=hoy + timedelta(days=10))
    ContratoServicio.objects.create(servicio_aplica="internet", proyecto=p, estado="firmado", fecha_fin=hoy + timedelta(days=200))
    ContratoServicio.objects.create(servicio_aplica="arriendo", proyecto=p, estado="terminado", fecha_fin=hoy + timedelta(days=5))

    r = atencion(hoy)["contratos_por_vencer"]
    assert r["total"] == 1
    assert r["items"][0]["url"] == f"/contratos/{pronto.id}"
    assert "en 10 días" in r["items"][0]["detalle"]


def test_atencion_plantas_en_operacion_sin_contrato_de_operacion():
    from apps.contratos.models import ContratoServicio
    from apps.plataforma.services.atencion import atencion
    from apps.proyectos.models import Proyecto

    hoy = _hoy()
    sin = Proyecto.objects.create(nombre_comercial="Sin contrato", estado="en_operacion")
    con = Proyecto.objects.create(nombre_comercial="Con mantenimiento", estado="en_operacion")
    Proyecto.objects.create(nombre_comercial="En desarrollo", estado="en_desarrollo")
    ContratoServicio.objects.create(servicio_aplica="mantenimiento", proyecto=con, estado="firmado")

    r = atencion(hoy)["plantas_sin_operacion"]
    assert [x["id"] for x in r["items"]] == [sin.id]


def test_atencion_arrendadores_activos_sin_cliente():
    from apps.arriendos.models import ArrArrendador
    from apps.clientes.models import Cliente
    from apps.contratos.models import ContratoServicio
    from apps.plataforma.services.atencion import atencion
    from apps.proyectos.models import Proyecto

    c = ContratoServicio.objects.create(servicio_aplica="arriendo", proyecto=Proyecto.objects.create(nombre_comercial="P"))
    ArrArrendador.objects.create(contrato=c, nombre="Sin ficha", activo=True)
    ArrArrendador.objects.create(contrato=c, nombre="Inactivo", activo=False)
    ArrArrendador.objects.create(contrato=c, nombre="Con ficha", activo=True, cliente=Cliente.objects.create(razon_social_nombre="X"))

    r = atencion(_hoy())["arrendadores_sin_cliente"]
    assert [x["titulo"] for x in r["items"]] == ["Sin ficha"]
    assert r["items"][0]["url"] == f"/contratos/{c.id}"
