"""Portafolios: un proyecto está en UN portafolio (Sara, 2026-10-15).

La pertenencia es `proyectos.portafolio_id`. La API: `/api/v1/portafolios` (lista,
crear, renombrar, borrar y `asignar`), `portafolio_id`/`portafolio_nombre` en el
detalle del proyecto y `?portafolio_id=` en su listado; el agrupamiento del
asistente de informes sale de la misma columna.
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
    """Dos portafolios y dos plantas en operación. Todo se revierte al final."""
    from django.db import transaction

    from apps.plataforma.models import Usuario
    from apps.proyectos.models import Portafolio, Proyecto

    atomica = transaction.atomic()
    atomica.__enter__()
    yield {
        "usuario": Usuario.objects.create(nombre="QA", email="qa@unergy.io", rol="admin",
                                          activo=True),
        "ayura": Portafolio.objects.create(nombre="Ayura"),
        "caribe": Portafolio.objects.create(nombre="Fondo Caribe"),
        "gandalf": Proyecto.objects.create(nombre_comercial="Valle de Gandalf",
                                           estado="en_operacion"),
        "perija": Proyecto.objects.create(nombre_comercial="Perija", estado="en_operacion"),
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


def _portafolios(datos, metodo, ruta="/api/v1/portafolios", cuerpo=None, accion=None, **kw):
    from api.v1.portafolios.views import PortafolioViewSet

    return _peticion(datos, PortafolioViewSet, metodo, ruta, cuerpo, accion, **kw)


def _asignar(datos, cuerpo):
    return _portafolios(datos, "patch", "/api/v1/portafolios/asignar", cuerpo, "asignar")


def _de(proyecto):
    proyecto.refresh_from_db()
    return proyecto.portafolio.nombre if proyecto.portafolio else None


# ── Un proyecto, un portafolio ────────────────────────────────────────────────

def test_ponerla_en_otro_portafolio_la_saca_del_que_tenia(datos):
    for portafolio in (datos["ayura"], datos["caribe"]):
        respuesta = _asignar(datos, {"proyecto_id": datos["gandalf"].id,
                                     "portafolio_id": portafolio.id})
        assert respuesta.status_code == 200, respuesta.data

    assert _de(datos["gandalf"]) == "Fondo Caribe"
    assert respuesta.data["portafolio_id"] == datos["caribe"].id


def test_null_la_deja_sin_portafolio(datos):
    from apps.proyectos.services import portafolios

    portafolios.mover(datos["gandalf"].id, datos["ayura"].id)
    _asignar(datos, {"proyecto_id": datos["gandalf"].id, "portafolio_id": None})

    assert _de(datos["gandalf"]) is None


def test_sacarla_de_un_portafolio_que_no_es_el_suyo_no_cambia_nada(datos):
    """`en_portafolio: false` es el cuerpo del front de varios portafolios."""
    from apps.proyectos.services import portafolios

    portafolios.mover(datos["gandalf"].id, datos["ayura"].id)
    _asignar(datos, {"proyecto_id": datos["gandalf"].id, "portafolio_id": datos["caribe"].id,
                     "en_portafolio": False})
    assert _de(datos["gandalf"]) == "Ayura"

    _asignar(datos, {"proyecto_id": datos["gandalf"].id, "portafolio_id": datos["ayura"].id,
                     "en_portafolio": False})
    assert _de(datos["gandalf"]) is None


# ── El listado y el borrado ───────────────────────────────────────────────────

def test_el_listado_trae_cada_capa_con_sus_plantas_y_el_pool(datos):
    from apps.proyectos.services import portafolios

    portafolios.mover(datos["gandalf"].id, datos["ayura"].id)

    respuesta = _portafolios(datos, "get", accion="list")

    assert respuesta.status_code == 200
    capas = {c["nombre"]: [p["nombre"] for p in c["proyectos"]]
             for c in respuesta.data["portafolios"]}
    assert capas == {"Ayura": ["Valle de Gandalf"], "Fondo Caribe": []}
    assert [p["nombre"] for p in respuesta.data["sin_portafolio"]] == ["Perija"]


def test_borrar_un_portafolio_deja_sus_plantas_sin_portafolio(datos):
    from apps.proyectos.models import Proyecto
    from apps.proyectos.services import portafolios

    portafolios.mover(datos["gandalf"].id, datos["ayura"].id)
    pk = datos["ayura"].id

    respuesta = _portafolios(datos, "delete", f"/api/v1/portafolios/{pk}", accion="destroy",
                             pk=str(pk))

    assert respuesta.status_code == 204
    assert Proyecto.objects.filter(pk=datos["gandalf"].id).exists()
    assert _de(datos["gandalf"]) is None


# ── Informes, proyectos y la fusión ───────────────────────────────────────────

def test_el_asistente_de_informes_agrupa_por_su_portafolio(datos):
    from apps.proyectos.services import portafolios

    portafolios.mover(datos["gandalf"].id, datos["ayura"].id)
    portafolios.mover(datos["perija"].id, datos["ayura"].id)

    assert portafolios.agrupamiento() == {"Ayura": ["Perija", "Valle de Gandalf"]}


def test_el_detalle_del_proyecto_trae_su_portafolio(datos):
    from api.v1.proyectos.serializers import ProyectoSerializer
    from apps.proyectos.services import portafolios

    portafolios.mover(datos["gandalf"].id, datos["caribe"].id)
    datos["gandalf"].refresh_from_db()

    salida = ProyectoSerializer(datos["gandalf"]).data

    assert (salida["portafolio_id"], salida["portafolio_nombre"]) == (
        datos["caribe"].id, "Fondo Caribe")


def test_la_fusion_de_proyectos_hereda_el_portafolio_si_el_ganador_no_tiene():
    from apps.proyectos.services import gestion

    assert "portafolio_id" in gestion.MERGE_ESCALAR_SI_VACIO
