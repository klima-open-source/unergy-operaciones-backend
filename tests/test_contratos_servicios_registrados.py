"""Qué servicios cubre un contrato: la tabla `servicios` y quién la escribe.

Plan `docs/refactor/08-plan-django-contratos.md`, §3: la escribe SOLO
`apps/contratos/services/servicios.py`, desde `Contrato.save()` —así ningún camino
de escritura la deja desalineada— y la API de contratos de servicio con la lista
explícita (`servicios`) cuando el formulario la manda.
"""
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

    from apps.plataforma.models import Usuario

    atomica = transaction.atomic()
    atomica.__enter__()
    usuario = Usuario.objects.create(nombre="QA", email="qa@unergy.io", rol="admin", activo=True)
    yield {"usuario": usuario}
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _registrados(contrato):
    from apps.contratos.models import Servicio

    return sorted(Servicio.objects.filter(contrato_id=contrato.pk)
                  .values_list("servicio", flat=True))


# ── Reglas (puras) ───────────────────────────────────────────────────────────


def test_un_ppa_cubre_solo_su_compra_o_venta():
    from apps.contratos.services import servicios

    ppa = SimpleNamespace(grupo="ppa", tipo_contrato="compra")
    assert servicios.deseados(ppa) == {"compra"}
    with pytest.raises(servicios.ServiciosInvalidos):
        servicios.deseados(ppa, ["venta"])


def test_operacion_cubre_solo_su_servicio_aplica():
    from apps.contratos.services import servicios

    om = SimpleNamespace(grupo="operacion", servicio_aplica="arriendo")
    assert servicios.deseados(om) == {"arriendo"}
    with pytest.raises(servicios.ServiciosInvalidos):
        servicios.deseados(om, ["arriendo", "internet"])


def test_representacion_acepta_uno_o_los_dos():
    """`servicio_aplica` vale siempre 'representacion' en este grupo: nombra al
    grupo, así que un contrato solo de CGM es válido."""
    from apps.contratos.services import servicios

    rep = SimpleNamespace(grupo="representacion_cgm", servicio_aplica="representacion")
    assert servicios.deseados(rep, ["representacion", "cgm"]) == {"representacion", "cgm"}
    assert servicios.deseados(rep, ["cgm"]) == {"cgm"}
    # Sin lista: conserva lo que tenga, sin agregarle nada; si es nuevo, representación.
    assert servicios.deseados(rep, None, {"cgm"}) == {"cgm"}
    assert servicios.deseados(rep, None, set()) == {"representacion"}
    for malo in ([], ["mantenimiento"]):
        with pytest.raises(servicios.ServiciosInvalidos):
            servicios.deseados(rep, malo)


# ── Escritura desde save() ───────────────────────────────────────────────────


def test_guardar_un_ppa_registra_y_sigue_su_tipo_contrato(datos):
    from apps.ppa.models import PpaContrato

    ppa = PpaContrato.objects.create(tipo_contrato="venta", nombre_interno="PPA")
    assert ppa.grupo == "ppa"
    assert _registrados(ppa) == ["venta"]

    ppa.tipo_contrato = "compra"
    ppa.save()
    assert _registrados(ppa) == ["compra"]  # nunca los dos


def test_guardar_un_contrato_de_operacion_registra_su_servicio(datos):
    from apps.contratos.models import ContratoServicio

    c = ContratoServicio.objects.create(servicio_aplica="mantenimiento")
    assert c.grupo == "operacion"
    assert _registrados(c) == ["mantenimiento"]

    c.servicio_aplica = "arriendo"
    c.save()
    assert c.grupo == "operacion"
    assert _registrados(c) == ["arriendo"]


def test_representacion_conserva_el_cgm_registrado_al_volver_a_guardar(datos):
    from apps.contratos.models import ContratoServicio
    from apps.contratos.services import servicios

    c = ContratoServicio.objects.create(servicio_aplica="representacion")
    assert _registrados(c) == ["representacion"]

    servicios.registrar(c, ["representacion", "cgm"])
    c.tarifa_cgm = None  # la tarifa no decide nada
    c.save()
    assert _registrados(c) == ["cgm", "representacion"]


def test_un_contrato_solo_de_cgm_no_gana_representacion_al_guardarse(datos):
    """El caso del contrato 152 (MGS 0011 El Roble): solo CGM."""
    from apps.contratos.models import ContratoServicio
    from apps.contratos.services import servicios

    c = ContratoServicio.objects.create(servicio_aplica="representacion")
    servicios.registrar(c, ["cgm"])
    c.save()
    assert _registrados(c) == ["cgm"]


# ── API ──────────────────────────────────────────────────────────────────────


def _pedir(metodo, url, datos_usuario, cuerpo=None, **kwargs):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.contratos_servicio.views import ContratoServicioViewSet

    factory = APIRequestFactory()
    peticion = getattr(factory, metodo)(url, cuerpo, format="json")
    force_authenticate(peticion, user=UsuarioAutenticado(datos_usuario["usuario"]))
    vista = ContratoServicioViewSet.as_view(kwargs.pop("acciones"))
    respuesta = vista(peticion, **kwargs)
    respuesta.render()
    return respuesta


def test_crear_un_contrato_de_representacion_y_cgm_por_la_api(datos):
    r = _pedir("post", "/api/v1/contratos-servicio", datos,
               {"servicio_aplica": "representacion", "servicios": ["representacion", "cgm"]},
               acciones={"post": "create"})
    assert r.status_code == 201, r.data
    assert r.data["subservicios"] == ["representacion", "cgm"]
    # Ni `grupo` ni columnas de PPA se cuelan en la API de servicio.
    assert "tipo_contrato" not in r.data and "comprador_nit" not in r.data


def test_crear_un_contrato_solo_de_cgm_por_la_api(datos):
    r = _pedir("post", "/api/v1/contratos-servicio", datos,
               {"servicio_aplica": "representacion", "servicios": ["cgm"]},
               acciones={"post": "create"})
    assert r.status_code == 201, r.data
    assert r.data["subservicios"] == ["cgm"]


def test_una_lista_que_no_es_del_grupo_es_un_400(datos):
    r = _pedir("post", "/api/v1/contratos-servicio", datos,
               {"servicio_aplica": "representacion", "servicios": ["mantenimiento"]},
               acciones={"post": "create"})
    assert r.status_code == 400
    assert "servicios" in r.data
