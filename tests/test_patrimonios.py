"""Patrimonios autónomos (modelo de Sara, 2026-10-14).

El inversionista es el dueño de su participación; si la tiene a través de un
patrimonio, al patrimonio se le factura. Jerarquía de dos niveles: general →
subpatrimonio. La API: `/api/v1/patrimonios` y `patrimonio_id` en la
participación de `/api/v1/proyectos/{id}/inversionistas`.
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


@pytest.fixture
def datos():
    """Fiduciaria Bancolombia (general, con ficha) → P.A. Valle de Gandalf (sin
    ficha propia), y una planta de Ayura. Todo se revierte al final."""
    from django.db import transaction

    from apps.clientes.models import Cliente
    from apps.plataforma.models import Usuario
    from apps.proyectos.models import Patrimonio, Proyecto

    atomica = transaction.atomic()
    atomica.__enter__()
    fiduciaria = Cliente.objects.create(
        razon_social_nombre="PATRIMONIOS AUTONOMOS FIDUCIARIA BANCOLOMBIA",
        nit_cedula="830.054.539-2",
    )
    general = Patrimonio.objects.create(nombre="Fiduciaria Bancolombia", cliente=fiduciaria)
    yield {
        "usuario": Usuario.objects.create(nombre="QA", email="qa@unergy.io", rol="admin",
                                          activo=True),
        "fiduciaria": fiduciaria,
        "general": general,
        "gandalf": Patrimonio.objects.create(nombre="P.A. Valle de Gandalf", codigo="17434",
                                             padre=general),
        "ayura": Cliente.objects.create(razon_social_nombre="Ayura S.A.S",
                                        nit_cedula="860.043.345-0"),
        "planta": Proyecto.objects.create(nombre_comercial="MGS 0004 Valle de Gandalf"),
    }
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _peticion(datos, vista, metodo, ruta, cuerpo=None, accion=None, **kwargs):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado

    peticion = getattr(APIRequestFactory(), metodo)(ruta, cuerpo, format="json")
    force_authenticate(peticion, user=UsuarioAutenticado(datos["usuario"]))
    respuesta = vista.as_view({metodo: accion or metodo})(peticion, **kwargs)
    respuesta.render()
    return respuesta


def _patrimonios(datos, metodo, ruta="/api/v1/patrimonios", cuerpo=None, accion=None, **kw):
    from api.v1.patrimonios.views import PatrimonioViewSet

    return _peticion(datos, PatrimonioViewSet, metodo, ruta, cuerpo, accion, **kw)


def _participacion(datos, cuerpo):
    from api.v1.proyectos.views import ProyectoViewSet

    pk = datos["planta"].id
    return _peticion(datos, ProyectoViewSet, "post", f"/api/v1/proyectos/{pk}/inversionistas",
                     cuerpo, "inversionistas", pk=str(pk))


# ── A quién se le factura ─────────────────────────────────────────────────────

def test_el_dueno_es_el_inversionista_y_se_factura_al_patrimonio(datos):
    from apps.proyectos.models import ProyectoInversionista
    from apps.proyectos.services import patrimonios

    p = ProyectoInversionista(proyecto=datos["planta"], patrimonio=datos["gandalf"])
    p.cliente = datos["ayura"]
    p.save()

    assert p.inversionista.cliente == datos["ayura"]
    # El subpatrimonio no tiene ficha propia: se le factura a la de su general.
    assert patrimonios.destinatario(p) == datos["fiduciaria"]


def test_sin_patrimonio_se_factura_al_inversionista(datos):
    from apps.proyectos.models import ProyectoInversionista
    from apps.proyectos.services import patrimonios

    p = ProyectoInversionista(proyecto=datos["planta"])
    p.cliente = datos["ayura"]
    p.save()

    assert patrimonios.destinatario(p) == datos["ayura"]


def test_un_subpatrimonio_con_ficha_propia_se_factura_a_la_suya(datos):
    from apps.clientes.models import Cliente
    from apps.proyectos.models import ProyectoInversionista
    from apps.proyectos.services import patrimonios

    sierra = Cliente.objects.create(razon_social_nombre="P.A. SOL DE LA SIERRA",
                                    nit_cedula="830.054.539-1")
    datos["gandalf"].cliente = sierra
    datos["gandalf"].save()
    p = ProyectoInversionista(proyecto=datos["planta"], patrimonio=datos["gandalf"])
    p.cliente = datos["ayura"]
    p.save()

    assert patrimonios.destinatario(p) == sierra


# ── La API de patrimonios ─────────────────────────────────────────────────────

def test_el_listado_es_el_arbol_con_sus_plantas(datos):
    from apps.proyectos.models import ProyectoInversionista

    p = ProyectoInversionista(proyecto=datos["planta"], patrimonio=datos["gandalf"])
    p.cliente = datos["ayura"]
    p.save()

    respuesta = _patrimonios(datos, "get", accion="list")

    assert respuesta.status_code == 200
    (general,) = respuesta.data
    assert general["nombre"] == "Fiduciaria Bancolombia"
    assert general["cliente_nit"] == "830.054.539-2"
    (sub,) = general["subpatrimonios"]
    assert (sub["codigo"], sub["padre_id"]) == ("17434", datos["general"].id)
    assert sub["participaciones"][0]["inversionista_nombre"] == "Ayura S.A.S"


def test_crear_un_subpatrimonio(datos):
    respuesta = _patrimonios(datos, "post", cuerpo={
        "nombre": "Sol de la Sierra", "codigo": "17844", "padre_id": datos["general"].id,
    }, accion="create")

    assert respuesta.status_code == 201, respuesta.data
    assert respuesta.data["padre_id"] == datos["general"].id


def test_un_codigo_repetido_da_409(datos):
    respuesta = _patrimonios(datos, "post", cuerpo={"nombre": "Otro", "codigo": "17434"},
                             accion="create")
    assert respuesta.status_code == 409


def test_un_subpatrimonio_no_cuelga_de_otro_subpatrimonio(datos):
    respuesta = _patrimonios(datos, "post", cuerpo={
        "nombre": "Nieto", "padre_id": datos["gandalf"].id,
    }, accion="create")
    assert respuesta.status_code == 409


def test_un_general_con_subpatrimonios_no_pasa_a_ser_subpatrimonio(datos):
    from apps.proyectos.models import Patrimonio

    otro = Patrimonio.objects.create(nombre="Fideicomisos BBVA")
    gid = datos["general"].id
    respuesta = _patrimonios(datos, "patch", f"/api/v1/patrimonios/{gid}",
                             {"padre_id": otro.id}, "partial_update", pk=str(gid))
    assert respuesta.status_code == 409


def test_no_se_borra_un_patrimonio_con_plantas_ni_con_subpatrimonios(datos):
    from apps.proyectos.models import ProyectoInversionista

    p = ProyectoInversionista(proyecto=datos["planta"], patrimonio=datos["gandalf"])
    p.cliente = datos["ayura"]
    p.save()

    for patrimonio in (datos["gandalf"], datos["general"]):
        respuesta = _patrimonios(datos, "delete", f"/api/v1/patrimonios/{patrimonio.id}",
                                 accion="destroy", pk=str(patrimonio.id))
        assert respuesta.status_code == 409


# ── La participación de la ficha del proyecto ─────────────────────────────────

def test_la_participacion_se_registra_a_traves_de_un_patrimonio(datos):
    respuesta = _participacion(datos, {
        "cliente_id": datos["ayura"].id, "porcentaje_participacion": 1,
        "patrimonio_id": datos["gandalf"].id,
    })

    assert respuesta.status_code == 201, respuesta.data
    assert respuesta.data["patrimonio_id"] == datos["gandalf"].id
    assert respuesta.data["patrimonio_nombre"] == "P.A. Valle de Gandalf"
    assert "es_patrimonio_autonomo" not in respuesta.data
