"""Tres correcciones del backend que salieron probando la migración del front (2026-10-08).

1. El nombre de la comunidad energética de un PPA se guarda: la API lo descartaba.
2. Una fecha fin anterior a la de inicio es un 400 con el motivo, no un 500.
3. El % de despacho de GESCON conserva sus decimales (fracción con 4 decimales).
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
    from django.db import transaction

    from apps.plataforma.models import Usuario

    atomica = transaction.atomic()
    atomica.__enter__()
    usuario = Usuario.objects.create(nombre="QA", email="qa@unergy.io", rol="admin", activo=True)
    yield {"usuario": usuario}
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _pedir(datos, vista, metodo, url, cuerpo, **kw):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado

    peticion = getattr(APIRequestFactory(), metodo)(url, cuerpo, format="json")
    force_authenticate(peticion, user=UsuarioAutenticado(datos["usuario"]))
    respuesta = vista(peticion, **kw)
    respuesta.render()
    return respuesta


def test_el_nombre_de_la_comunidad_se_guarda(datos):
    from api.v1.ppa.views import PpaViewSet
    from apps.ppa.models import PpaContrato

    respuesta = _pedir(datos, PpaViewSet.as_view({"post": "create"}), "post", "/api/v1/ppa", {
        "nombre_interno": "PPA comunidad", "tipo_contrato": "venta",
        "es_comunidad_energetica": True, "nombre_comunidad": "Comunidad Solar del Valle",
    })

    assert respuesta.status_code == 201, respuesta.data
    contrato = PpaContrato.objects.get(pk=respuesta.data["id"])
    assert contrato.nombre_comunidad == "Comunidad Solar del Valle"
    assert respuesta.data["nombre_comunidad"] == "Comunidad Solar del Valle"


def test_ppa_con_fecha_fin_anterior_es_un_400(datos):
    from api.v1.ppa.views import PpaViewSet

    respuesta = _pedir(datos, PpaViewSet.as_view({"post": "create"}), "post", "/api/v1/ppa", {
        "nombre_interno": "PPA fechas", "tipo_contrato": "venta",
        "fecha_inicio": "2026-06-01", "fecha_fin": "2026-01-01",
    })

    assert respuesta.status_code == 400, respuesta.data
    assert "fecha_fin" in respuesta.data


def test_editar_solo_la_fecha_fin_compara_con_la_guardada(datos):
    """Un PATCH que solo trae la fecha fin: la de inicio es la guardada."""
    from api.v1.contratos_servicio.views import ContratoServicioViewSet
    from apps.contratos.models import ContratoServicio

    contrato = ContratoServicio.objects.create(
        servicio_aplica="mantenimiento", fecha_inicio="2026-06-01", fecha_fin="2027-06-01",
    )
    vista = ContratoServicioViewSet.as_view({"patch": "partial_update"})
    url = f"/api/v1/contratos-servicio/{contrato.pk}"

    malo = _pedir(datos, vista, "patch", url, {"fecha_fin": "2026-01-01"}, pk=contrato.pk)
    bueno = _pedir(datos, vista, "patch", url, {"fecha_fin": "2028-01-01"}, pk=contrato.pk)

    assert malo.status_code == 400, malo.data
    assert "fecha_fin" in malo.data
    assert bueno.status_code == 200, bueno.data


def test_el_porcentaje_de_despacho_conserva_sus_decimales(datos):
    from decimal import Decimal

    from apps.mercado_xm.models import AsicSolicitud

    campo = AsicSolicitud._meta.get_field("porcentaje_despacho")
    # 33,33 % viaja como 0,3333: con 2 decimales se guardaba 0,33.
    assert campo.decimal_places >= 4
    assert campo.to_python("0.3333") == Decimal("0.3333")
