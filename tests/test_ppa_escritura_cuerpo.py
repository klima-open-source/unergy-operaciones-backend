"""El CUERPO de `POST`/`PATCH /ppa`, no solo su ruta.

`test_paridad_urls.py` compara las tablas de rutas de los dos backends y no mira
lo que viaja adentro. Entre el port a Django (2026-09-04) y el 2026-09-17,
`POST /ppa` aceptó el cuerpo del frontend, respondió **201** y descartó en
silencio `comprador_id`, `vendedor_id` y `responsable_id`: el serializer los
declaraba con el nombre del ORM (`comprador`, `vendedor`, `responsable`) y DRF
ignora las claves que no reconoce. El contrato quedaba sin partes y sin
responsable, y nadie lo veía porque ninguna prueba mandaba un cuerpo.

Estas pruebas fijan el contrato con el nombre que la LECTURA ya devolvía y que
`PPAContratoCreate` de FastAPI recibía. Ver `docs/DIAGNOSTICO_PPA.md` §3.

El aislamiento en SQLite es el mismo de `test_clientes_duplicado_y_nit.py`: se
crea una base en memoria y se comprueba que no sea la real.
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
    """Un comprador, un vendedor y un responsable: las tres relaciones que el
    wizard deja elegir. Todo dentro de una transacción que se revierte."""
    from django.db import transaction

    from apps.clientes.models import Cliente
    from apps.plataforma.models import Usuario
    from apps.ppa.models import PpaResponsable

    atomica = transaction.atomic()
    atomica.__enter__()
    valores = {
        "usuario": Usuario.objects.create(
            nombre="QA", email="qa@unergy.io", rol="admin", activo=True
        ),
        "comprador": Cliente.objects.create(
            razon_social_nombre="Comprador S.A.S.", nit_cedula="900000001-1"
        ),
        "vendedor": Cliente.objects.create(
            razon_social_nombre="Vendedor S.A.S.", nit_cedula="900000002-2"
        ),
        "responsable": PpaResponsable.objects.create(nombre="Unergy Pruebas"),
    }
    yield valores
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


CUERPO_MINIMO = {
    "numero_codigo_contrato": "UNERGY-TEST-001",
    "nombre_interno": "Planta de prueba",
    "fecha_inicio": "2026-01-01",
    "fecha_fin": "2030-12-31",
    "tipo_contrato": "compra",
}


def _peticion(datos, metodo, ruta, cuerpo, accion, **kwargs):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.ppa.views import PpaViewSet

    peticion = getattr(APIRequestFactory(), metodo)(ruta, cuerpo, format="json")
    force_authenticate(peticion, user=UsuarioAutenticado(datos["usuario"]))
    respuesta = PpaViewSet.as_view({metodo: accion})(peticion, **kwargs)
    respuesta.render()
    return respuesta


def _crear(datos, cuerpo):
    return _peticion(datos, "post", "/api/v1/ppa", cuerpo, "create")


def _editar(datos, pk, cuerpo):
    return _peticion(
        datos, "patch", f"/api/v1/ppa/{pk}", cuerpo, "partial_update", pk=str(pk)
    )


# ── Las tres relaciones: la regresión ────────────────────────────────────────

def test_crear_conserva_las_tres_relaciones(datos):
    """El cuerpo tal cual lo manda `PPAContratoWizard.vue`. Antes devolvía 201
    con las tres columnas en null."""
    from apps.ppa.models import PpaContrato

    respuesta = _crear(datos, {
        **CUERPO_MINIMO,
        "comprador_id": datos["comprador"].id,
        "vendedor_id": datos["vendedor"].id,
        "responsable_id": datos["responsable"].id,
    })

    assert respuesta.status_code == 201, respuesta.data
    contrato = PpaContrato.objects.get(pk=respuesta.data["id"])
    assert contrato.comprador_id == datos["comprador"].id
    assert contrato.vendedor_id == datos["vendedor"].id
    assert contrato.responsable_id == datos["responsable"].id


def test_la_respuesta_devuelve_las_relaciones_que_recibio(datos):
    """Lo que entra por `<rol>_id` sale por `<rol>_id`: si el front no ve de
    vuelta lo que mandó, no tiene cómo detectar que se perdió."""
    respuesta = _crear(datos, {
        **CUERPO_MINIMO,
        "comprador_id": datos["comprador"].id,
        "responsable_id": datos["responsable"].id,
    })

    assert respuesta.data["comprador_id"] == datos["comprador"].id
    assert respuesta.data["responsable_id"] == datos["responsable"].id


def test_editar_asigna_una_parte_que_faltaba(datos):
    """El PATCH es el camino del backfill desde la UI."""
    from apps.ppa.models import PpaContrato

    creado = _crear(datos, CUERPO_MINIMO)
    pk = creado.data["id"]

    respuesta = _editar(datos, pk, {"comprador_id": datos["comprador"].id})

    assert respuesta.status_code == 200, respuesta.data
    assert PpaContrato.objects.get(pk=pk).comprador_id == datos["comprador"].id


def test_editar_con_null_desasigna(datos):
    from apps.ppa.models import PpaContrato

    creado = _crear(datos, {**CUERPO_MINIMO, "vendedor_id": datos["vendedor"].id})
    pk = creado.data["id"]

    respuesta = _editar(datos, pk, {"vendedor_id": None})

    assert respuesta.status_code == 200, respuesta.data
    assert PpaContrato.objects.get(pk=pk).vendedor_id is None


def test_un_id_que_no_existe_es_400_y_no_un_descarte(datos):
    """Antes, un id inválido se ignoraba igual que uno válido."""
    respuesta = _crear(datos, {**CUERPO_MINIMO, "comprador_id": 10**9})

    assert respuesta.status_code == 400
    assert "comprador_id" in respuesta.data


def test_las_tres_son_opcionales(datos):
    """Un PPA sin partes se sigue creando: hay 27 así en producción y el wizard
    no las exige."""
    respuesta = _crear(datos, CUERPO_MINIMO)

    assert respuesta.status_code == 201, respuesta.data
    assert respuesta.data["comprador_id"] is None
    assert respuesta.data["responsable_id"] is None


# ── Que el arreglo no se coma lo que ya funcionaba ───────────────────────────

def test_sincronizar_partes_copia_nombre_y_nit_del_cliente(datos):
    """Con la FK puesta, el servicio ya duplicaba nombre y NIT en el contrato.
    Sin FK nunca corría, que es por lo que 33 contratos tienen el nombre escrito
    a mano y solo 7 tienen `comprador_id`."""
    respuesta = _crear(datos, {
        **CUERPO_MINIMO, "comprador_id": datos["comprador"].id,
    })

    assert respuesta.data["comprador_nombre"] == "Comprador S.A.S."
    assert respuesta.data["comprador_nit"] == "900000001-1"


def test_el_resto_del_cuerpo_del_wizard_sobrevive(datos):
    """Las claves que nunca se perdieron siguen llegando."""
    respuesta = _crear(datos, {
        **CUERPO_MINIMO,
        "tarifa_base": 300,
        "indice_indexacion": "IPP",
        "periodo_indexacion_base": "2025-12",
        "cantidad_minima_kwh_mes": 1000,
        "carpeta_link": "https://drive.google.com/x",
    })

    assert respuesta.status_code == 201, respuesta.data
    assert respuesta.data["indice_indexacion"] == "IPP"
    assert respuesta.data["periodo_indexacion_base"] == "2025-12"
    assert respuesta.data["carpeta_link"] == "https://drive.google.com/x"


def test_la_lectura_y_la_escritura_nombran_igual_las_relaciones(datos):
    """El desajuste que causó todo: la lectura devolvía `<rol>_id` y la
    escritura esperaba `<rol>`. Si vuelven a divergir, esto falla."""
    from api.v1.ppa import serializers as ppa_serializers

    escritura = set(ppa_serializers.ContratoEscrituraSerializer().fields)
    lectura = set(ppa_serializers.ContratoSerializer().fields)

    for campo in ("comprador_id", "vendedor_id", "responsable_id"):
        assert campo in escritura, f"la escritura no acepta {campo}"
        assert campo in lectura, f"la lectura no devuelve {campo}"
