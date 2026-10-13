"""Crear un PPA avisa si ya hay uno vivo que cubre lo mismo.

`ContratoServicio` avisa desde el 2026-09-19; el PPA era el único de los tres
grupos de servicio sin protección, y por eso los dos wizards no se comportaban
igual ante lo mismo.

Dos señales: el mismo **número de contrato** --la identidad del documento-- o la
misma **contraparte + tipo + al menos una planta en común**, que cubre el PPA
registrado dos veces sin número o con el número escrito distinto.

Como en contratos de servicio, lo delicado es no estorbar lo legítimo: un PPA de
compra y otro de venta sobre la misma planta son contratos distintos, y un PPA
terminado no debe estorbar a su reemplazo.
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


CUERPO = {
    "nombre_interno": "PPA QA",
    "fecha_inicio": "2026-01-01",
    "fecha_fin": "2030-12-31",
    "tipo_contrato": "venta",
}


def _crear(datos_usuario, cuerpo, forzar=False):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.ppa.views import PpaViewSet

    url = "/api/v1/ppa" + ("?forzar=true" if forzar else "")
    peticion = APIRequestFactory().post(url, cuerpo, format="json")
    force_authenticate(peticion, user=UsuarioAutenticado(datos_usuario["usuario"]))
    respuesta = PpaViewSet.as_view({"post": "create"})(peticion)
    respuesta.render()
    return respuesta


def _planta(nombre="Planta QA"):
    from apps.proyectos.models import Proyecto

    return Proyecto.objects.create(nombre_comercial=nombre)


def _ppa(plantas=(), **kw):
    from apps.ppa.models import PpaContrato, PpaContratoProyecto

    kw.setdefault("tipo_contrato", "venta")
    kw.setdefault("fecha_fin", "2030-12-31")
    contrato = PpaContrato.objects.create(**kw)
    for planta in plantas:
        PpaContratoProyecto.objects.create(contrato=contrato, proyecto=planta)
    return contrato


def _cliente(nombre):
    from apps.clientes.models import Cliente

    return Cliente.objects.create(razon_social_nombre=nombre)


# ── Avisa cuando debe ─────────────────────────────────────────────────────────

def test_el_mismo_numero_de_contrato_avisa(datos):
    _ppa(numero_codigo_contrato="UNERGY-PPA-014")

    respuesta = _crear(datos, {**CUERPO, "numero_codigo_contrato": "UNERGY-PPA-014"})

    assert respuesta.status_code == 409, respuesta.data
    assert respuesta.data["detail"]["duplicado_contrato"] is True


def test_el_numero_se_compara_normalizado(datos):
    """"unergy ppa 014" y "UNERGY-PPA-014" son el mismo documento."""
    _ppa(numero_codigo_contrato="UNERGY-PPA-014")

    respuesta = _crear(datos, {**CUERPO, "numero_codigo_contrato": "unergy ppa 014"})

    assert respuesta.status_code == 409, respuesta.data


def test_sin_numero_avisa_por_contraparte_y_planta(datos):
    planta = _planta()
    vendedor = _cliente("Generadora del Cauca")
    _ppa(plantas=[planta], vendedor=vendedor)

    respuesta = _crear(datos, {
        **CUERPO, "proyecto_ids": [planta.id],
        "vendedor_id": vendedor.id, "vendedor_nombre": "Generadora del Cauca",
    })

    assert respuesta.status_code == 409, respuesta.data


# ── NO estorba lo legítimo ────────────────────────────────────────────────────

def test_compra_y_venta_sobre_la_misma_planta_conviven(datos):
    """Son contratos distintos: Unergy compra por uno y vende por el otro."""
    planta = _planta()
    vendedor = _cliente("Generadora del Cauca")
    _ppa(plantas=[planta], tipo_contrato="compra",
         vendedor=vendedor)

    respuesta = _crear(datos, {
        **CUERPO, "tipo_contrato": "venta", "proyecto_ids": [planta.id],
        "vendedor_id": vendedor.id, "vendedor_nombre": "Generadora del Cauca",
    })

    assert respuesta.status_code == 201, respuesta.data


def test_un_ppa_vencido_no_estorba_a_su_reemplazo(datos):
    from datetime import timedelta

    from apps.plataforma.services.fechas import hoy_col

    _ppa(numero_codigo_contrato="UNERGY-PPA-014",
         fecha_fin=hoy_col() - timedelta(days=1))

    respuesta = _crear(datos, {**CUERPO, "numero_codigo_contrato": "UNERGY-PPA-014"})

    assert respuesta.status_code == 201, respuesta.data


def test_un_ppa_borrado_tampoco(datos):
    from django.utils import timezone

    contrato = _ppa(numero_codigo_contrato="UNERGY-PPA-014")
    contrato.deleted_at = timezone.now()
    contrato.save(update_fields=["deleted_at"])

    respuesta = _crear(datos, {**CUERPO, "numero_codigo_contrato": "UNERGY-PPA-014"})

    assert respuesta.status_code == 201, respuesta.data


def test_otra_planta_con_la_misma_contraparte_no_avisa(datos):
    vendedor = _cliente("Generadora del Cauca")
    _ppa(plantas=[_planta("Una")], vendedor=vendedor)

    respuesta = _crear(datos, {
        **CUERPO, "proyecto_ids": [_planta("Otra").id],
        "vendedor_id": vendedor.id, "vendedor_nombre": "Generadora del Cauca",
    })

    assert respuesta.status_code == 201, respuesta.data


def test_sin_numero_ni_plantas_no_se_compara_nada(datos):
    """Hay 10 contratos sin planta en producción: no hay con qué situarlos."""
    _ppa(plantas=[_planta()])

    respuesta = _crear(datos, CUERPO)

    assert respuesta.status_code == 201, respuesta.data


def test_forzar_crea_de_todos_modos(datos):
    _ppa(numero_codigo_contrato="UNERGY-PPA-014")

    respuesta = _crear(
        datos, {**CUERPO, "numero_codigo_contrato": "UNERGY-PPA-014"}, forzar=True,
    )

    assert respuesta.status_code == 201, respuesta.data


# ── El camino del CRM ────────────────────────────────────────────────────────
# `firmar()` crea el PPA con `crear_ppa` y firma desde una pantalla que no tiene
# dónde confirmar "crear igual": un 409 dejaría la firma sin salida. Por eso ahí
# el duplicado viaja como AVISO, que es el canal que el CRM ya usa para contar lo
# que quedó cojo.

def test_crear_ppa_avisa_del_duplicado_en_vez_de_bloquear(datos):
    from apps.ppa.services.escritura import crear_ppa

    _ppa(numero_codigo_contrato="UNERGY-PPA-014")

    resultado = crear_ppa(
        tipo_contrato="venta",
        datos={"numero_codigo_contrato": "UNERGY-PPA-014", "nombre_interno": "PPA QA"},
    )

    assert resultado.contrato.id is not None, "la firma no se puede bloquear"
    assert any("Ya existe un contrato PPA" in a for a in resultado.avisos), resultado.avisos


def test_sin_duplicado_no_hay_aviso_de_repetido(datos):
    from apps.ppa.services.escritura import crear_ppa

    resultado = crear_ppa(
        tipo_contrato="venta",
        datos={"numero_codigo_contrato": "UNERGY-PPA-099", "nombre_interno": "PPA QA"},
    )

    assert not any("Ya existe un contrato PPA" in a for a in resultado.avisos)
