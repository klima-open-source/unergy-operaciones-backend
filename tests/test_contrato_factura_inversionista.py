"""La factura de un contrato dice a QUÉ CLIENTE se le emite.

`contrato_factura.inversionista` era texto escrito a mano: sin NIT y sin forma de
saber a quién corresponde. La misma enfermedad de las partes de un contrato
(§4-decies), una capa más abajo -- y en facturas, donde el NIT hace falta de
verdad para emitirlas.

Queda como en `ContratoServicio`: `inversionista` es el vínculo,
`inversionista_nombre` la copia del texto (decisión de Sara, 2026-09-20: dos
nombres distintos para la misma cosa generan ambigüedad).

De paso se arregla un fallo que llevaba tiempo: `FacturaSerializer` exponía
`inversionista_id` --que no existía en el modelo, así que salía siempre `None`--
y NO mandaba el nombre. La pantalla de facturas lee ese campo, de modo que su
columna "Inversionista" mostraba "—" aunque el dato estuviera guardado.
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


def _pedir(metodo, url, datos_usuario, cuerpo=None, **kwargs):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.contratos_servicio.views import ContratoServicioViewSet

    factory = APIRequestFactory()
    peticion = getattr(factory, metodo)(url, cuerpo, format="json") if cuerpo is not None \
        else getattr(factory, metodo)(url)
    force_authenticate(peticion, user=UsuarioAutenticado(datos_usuario["usuario"]))
    vista = ContratoServicioViewSet.as_view(kwargs.pop("acciones"))
    respuesta = vista(peticion, **kwargs)
    respuesta.render()
    return respuesta


def _contrato():
    from apps.contratos.models import ContratoServicio

    return ContratoServicio.objects.create(servicio_aplica="mantenimiento")


def _cliente(nombre, nit=None):
    from apps.clientes.models import Cliente

    return Cliente.objects.create(razon_social_nombre=nombre, nit_cedula=nit)


def test_crear_una_factura_vinculada_al_cliente(datos):
    from apps.facturacion.models import ContratoFactura

    contrato = _contrato()
    cliente = _cliente("Fondo Solar Uno S.A.S.", "902555666-7")

    respuesta = _pedir(
        "post", f"/api/v1/contratos-servicio/{contrato.id}/facturas", datos,
        cuerpo={
            "tipo": "inversionista", "fecha": "2026-07",
            "inversionista_id": cliente.id, "monto": 1_500_000,
        },
        acciones={"post": "facturas"}, pk=contrato.id,
    )

    assert respuesta.status_code == 201, respuesta.data
    guardada = ContratoFactura.objects.get(pk=respuesta.data["id"])
    assert guardada.inversionista_id == cliente.id


def test_el_nombre_se_copia_del_cliente(datos):
    """El vínculo manda: deja de haber dos grafías del mismo inversionista según
    quién registró la factura."""
    from apps.facturacion.models import ContratoFactura

    contrato = _contrato()
    cliente = _cliente("Fondo Solar Uno S.A.S.")

    respuesta = _pedir(
        "post", f"/api/v1/contratos-servicio/{contrato.id}/facturas", datos,
        cuerpo={
            "tipo": "inversionista", "fecha": "2026-07",
            "inversionista_id": cliente.id,
            "inversionista_nombre": "fondo solar 1",   # lo que alguien tecleó
            "monto": 1_000,
        },
        acciones={"post": "facturas"}, pk=contrato.id,
    )

    assert respuesta.status_code == 201, respuesta.data
    guardada = ContratoFactura.objects.get(pk=respuesta.data["id"])
    assert guardada.inversionista_nombre == "Fondo Solar Uno S.A.S."


def test_el_listado_devuelve_el_nombre(datos):
    """Lo que la pantalla necesita para su columna, y que la API nunca mandaba:
    salía `inversionista_id` (que ni existía en el modelo) y nada más."""
    from apps.facturacion.models import ContratoFactura

    contrato = _contrato()
    cliente = _cliente("Fondo Solar Uno S.A.S.")
    ContratoFactura.objects.create(
        contrato=contrato, tipo="inversionista", fecha="2026-07",
        inversionista=cliente, inversionista_nombre="Fondo Solar Uno S.A.S.",
        monto=1_000,
    )

    respuesta = _pedir(
        "get", f"/api/v1/contratos-servicio/{contrato.id}/facturas", datos,
        acciones={"get": "facturas"}, pk=contrato.id,
    )

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data[0]["inversionista_nombre"] == "Fondo Solar Uno S.A.S."
    assert respuesta.data[0]["inversionista_id"] == cliente.id


def test_una_factura_de_solenium_no_necesita_inversionista(datos):
    """Las de Solenium no van a un inversionista: la otra mitad de esta pantalla."""
    contrato = _contrato()

    respuesta = _pedir(
        "post", f"/api/v1/contratos-servicio/{contrato.id}/facturas", datos,
        cuerpo={"tipo": "solenium", "fecha": "2026-07", "monto": 500},
        acciones={"post": "facturas"}, pk=contrato.id,
    )

    assert respuesta.status_code == 201, respuesta.data


def test_las_facturas_viejas_conservan_su_nombre(datos):
    """El renombrado no puede perder lo que ya estaba escrito: son 343 filas."""
    from apps.facturacion.models import ContratoFactura

    contrato = _contrato()
    factura = ContratoFactura.objects.create(
        contrato=contrato, tipo="inversionista", fecha="2026-01",
        inversionista_nombre="Escrito A Mano", monto=1,
    )

    assert factura.inversionista_id is None
    assert ContratoFactura.objects.get(pk=factura.id).inversionista_nombre == "Escrito A Mano"
