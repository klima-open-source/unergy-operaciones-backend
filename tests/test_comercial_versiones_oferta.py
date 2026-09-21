"""Versiones de una oferta: reofertar sin borrar la propuesta anterior.

Hasta ahora la oferta era una sola fila que se mutaba con `PATCH`: un
`documento_url`, un `precio_detalle` de texto libre, y cero historial. Reofertar
era sobrescribir. Ver `docs/DOMINIO_COMERCIAL.md`, O-8 y O-9.

Lo que se fija acá:

- las versiones son **append-only** y se numeran solas;
- la tabla de precio por año es estructurada, no texto (es lo único que el
  contrato necesitaba y la oferta no tenía);
- **como mucho una versión aceptada** por oferta: es la que dirá con qué
  condiciones nace el PPA, y dos harían esa respuesta ambigua.
"""
import datetime as dt

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

    from apps.clientes.models import Cliente
    from apps.comercial.models import Oportunidad, OportunidadOferta
    from apps.plataforma.models import Usuario

    atomica = transaction.atomic()
    atomica.__enter__()

    usuario = Usuario.objects.create(
        nombre="QA", email="qa@unergy.io", rol="admin", activo=True
    )
    cliente = Cliente.objects.create(
        razon_social_nombre="Generador S.A.S.", nit_cedula="900000009-9"
    )
    oportunidad = Oportunidad.objects.create(cliente_id=cliente.id, nombre="Balmora")
    oferta = OportunidadOferta.objects.create(
        oportunidad_id=oportunidad.id, tipo="compra_energia",
        numero_oferta="OP.COM No.0099-9-2026",
    )

    yield {"usuario": usuario, "oferta": oferta}

    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


# La tabla 2 del PDF de la oferta: precio por año del período de suministro.
PRECIOS = [
    {"anio": 2026, "precio": 330}, {"anio": 2027, "precio": 318},
    {"anio": 2028, "precio": 318}, {"anio": 2029, "precio": 306},
]


def _agregar(datos, **extra):
    from apps.comercial.services import versiones

    cuerpo = {
        "fecha_envio": dt.date(2026, 5, 20),
        "documento_url": "https://drive/oferta-v1.pdf",
        "indice_indexacion": "IPP serie Oferta Interna provisional",
        "periodo_indexacion_base": "2026-05",
        "precios": PRECIOS,
    }
    cuerpo.update(extra)
    return versiones.agregar(datos["oferta"], cuerpo, datos["usuario"])


# ── Numeración y append-only ─────────────────────────────────────────────────

def test_la_primera_version_es_la_uno(datos):
    version = _agregar(datos)

    assert version.numero == 1
    assert version.oferta_id == datos["oferta"].id


def test_reofertar_agrega_una_version_y_no_toca_la_anterior(datos):
    v1 = _agregar(datos, documento_url="https://drive/v1.pdf")
    v2 = _agregar(datos, documento_url="https://drive/v2.pdf",
                  precios=[{"anio": 2026, "precio": 320}],
                  que_cambio="Bajamos el precio del primer año")

    assert (v1.numero, v2.numero) == (1, 2)
    v1.refresh_from_db()
    assert v1.documento_url == "https://drive/v1.pdf", "la v1 no se toca"
    assert v1.precios.count() == 4, "la v1 conserva su tabla de precios"


def test_el_consecutivo_de_la_oferta_no_cambia_al_reofertar(datos):
    """La identidad de la oferta es su número, y es estable: es la llave con la
    que después se emparejan los correos."""
    _agregar(datos)
    _agregar(datos)

    datos["oferta"].refresh_from_db()
    assert datos["oferta"].numero_oferta == "OP.COM No.0099-9-2026"


def test_dos_versiones_no_pueden_repetir_numero(datos):
    """El unico `(oferta, numero)` es la red por si el maximo se lee dos veces."""
    from django.db import IntegrityError

    from apps.comercial.models import OportunidadOfertaVersion

    _agregar(datos)

    with pytest.raises(IntegrityError):
        OportunidadOfertaVersion.objects.create(oferta_id=datos["oferta"].id, numero=1)


def test_la_ultima_es_la_de_numero_mas_alto(datos):
    from apps.comercial.services import versiones

    _agregar(datos)
    v2 = _agregar(datos, fecha_envio=None)   # un borrador reciente

    assert versiones.ultima(datos["oferta"]).id == v2.id


# ── La tabla de precios ──────────────────────────────────────────────────────

def test_los_precios_se_guardan_por_anio(datos):
    version = _agregar(datos)

    filas = {p.anio: float(p.precio) for p in version.precios.all()}
    assert filas == {2026: 330.0, 2027: 318.0, 2028: 318.0, 2029: 306.0}


def test_una_version_puede_no_traer_precios(datos):
    """Las 165 ofertas historicas van a quedar asi: su precio es texto libre y
    no se puede parsear con confianza."""
    version = _agregar(datos, precios=[])

    assert version.precios.count() == 0


def test_un_anio_repetido_no_entra(datos):
    from api.exceptions import NoProcesable

    with pytest.raises(NoProcesable):
        _agregar(datos, precios=[
            {"anio": 2026, "precio": 330}, {"anio": 2026, "precio": 318},
        ])


def test_se_guardan_el_indexador_y_el_mes_base(datos):
    """Los dos datos que el contrato necesita y la oferta no tenia. En el PDF
    son "Indicador de Actualizacion" y la fila "Precio Base"."""
    version = _agregar(datos)

    assert version.indice_indexacion == "IPP serie Oferta Interna provisional"
    assert version.periodo_indexacion_base == "2026-05"


# ── La versión aceptada: la que se firma ─────────────────────────────────────

def test_aceptar_marca_la_version(datos):
    from apps.comercial.services import versiones

    version = _agregar(datos)

    versiones.aceptar(version, dt.date(2026, 6, 1))

    version.refresh_from_db()
    assert version.fecha_aceptacion == dt.date(2026, 6, 1)
    assert versiones.aceptada(datos["oferta"]).id == version.id


def test_no_se_pueden_aceptar_dos_versiones(datos):
    """Con dos aceptadas no se sabria con que condiciones nace el contrato, que
    es justo lo que esta tabla viene a resolver."""
    from api.exceptions import Conflict
    from apps.comercial.services import versiones

    v1 = _agregar(datos)
    v2 = _agregar(datos)
    versiones.aceptar(v1, dt.date(2026, 6, 1))

    with pytest.raises(Conflict):
        versiones.aceptar(v2, dt.date(2026, 6, 15))


def test_aceptar_dos_veces_la_misma_no_falla(datos):
    """Idempotente: corregir la fecha de aceptacion no es un conflicto."""
    from apps.comercial.services import versiones

    version = _agregar(datos)
    versiones.aceptar(version, dt.date(2026, 6, 1))
    versiones.aceptar(version, dt.date(2026, 6, 2))

    version.refresh_from_db()
    assert version.fecha_aceptacion == dt.date(2026, 6, 2)


def test_un_borrador_no_se_puede_aceptar(datos):
    """Sin fecha de envio la propuesta no salio: el cliente no pudo aceptarla."""
    from api.exceptions import NoProcesable
    from apps.comercial.services import versiones

    borrador = _agregar(datos, fecha_envio=None)

    with pytest.raises(NoProcesable):
        versiones.aceptar(borrador, dt.date(2026, 6, 1))


def test_sin_versiones_no_hay_ultima_ni_aceptada(datos):
    from apps.comercial.services import versiones

    assert versiones.ultima(datos["oferta"]) is None
    assert versiones.aceptada(datos["oferta"]) is None


# ── Que borrar la oferta se lleve sus versiones ──────────────────────────────

def test_borrar_la_oferta_borra_sus_versiones(datos):
    from apps.comercial.models import (
        OportunidadOfertaVersion, OportunidadOfertaVersionPrecio,
    )

    version = _agregar(datos)
    datos["oferta"].delete()

    assert not OportunidadOfertaVersion.objects.filter(pk=version.id).exists()
    assert not OportunidadOfertaVersionPrecio.objects.filter(
        version_id=version.id
    ).exists()


# ── Los endpoints ────────────────────────────────────────────────────────────

def _peticion(datos, metodo, ruta, cuerpo, accion, **kwargs):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.comercial.views import ComercialViewSet

    peticion = getattr(APIRequestFactory(), metodo)(ruta, cuerpo, format="json")
    force_authenticate(peticion, user=UsuarioAutenticado(datos["usuario"]))
    respuesta = ComercialViewSet.as_view({metodo: accion})(peticion, **kwargs)
    respuesta.render()
    return respuesta


def _post_version(datos, cuerpo):
    oid = datos["oferta"].id
    return _peticion(
        datos, "post", f"/api/v1/comercial/ofertas/{oid}/versiones", cuerpo,
        "oferta_versiones", oferta_id=str(oid),
    )


def _get_versiones(datos):
    oid = datos["oferta"].id
    return _peticion(
        datos, "get", f"/api/v1/comercial/ofertas/{oid}/versiones", None,
        "oferta_versiones", oferta_id=str(oid),
    )


def _post_aceptar(datos, numero, cuerpo):
    oid = datos["oferta"].id
    return _peticion(
        datos, "post", f"/api/v1/comercial/ofertas/{oid}/versiones/{numero}/aceptar",
        cuerpo, "aceptar_version", oferta_id=str(oid), numero=str(numero),
    )


CUERPO_V1 = {
    "fecha_envio": "2026-05-20",
    "documento_url": "https://drive/oferta-v1.pdf",
    "indice_indexacion": "IPP serie Oferta Interna provisional",
    "periodo_indexacion_base": "2026-05",
    "precios": [{"anio": 2026, "precio": 330}, {"anio": 2027, "precio": 318}],
}


def test_post_crea_la_version_con_su_tabla(datos):
    respuesta = _post_version(datos, CUERPO_V1)

    assert respuesta.status_code == 201, respuesta.data
    assert respuesta.data["numero"] == 1
    assert respuesta.data["precios"] == [
        {"anio": 2026, "precio": 330.0}, {"anio": 2027, "precio": 318.0},
    ]
    assert respuesta.data["periodo_indexacion_base"] == "2026-05"


def test_el_numero_no_lo_manda_el_cliente(datos):
    """Es el orden de la propuesta dentro de su oferta, no un dato que alguien
    elija. Mandarlo no cambia nada."""
    respuesta = _post_version(datos, {**CUERPO_V1, "numero": 77})

    assert respuesta.data["numero"] == 1


def test_el_listado_devuelve_la_mas_nueva_primero(datos):
    _post_version(datos, CUERPO_V1)
    _post_version(datos, {**CUERPO_V1, "que_cambio": "Bajamos el precio"})

    respuesta = _get_versiones(datos)

    assert respuesta.status_code == 200
    assert [v["numero"] for v in respuesta.data] == [2, 1]


def test_una_version_sin_fecha_de_envio_es_borrador(datos):
    respuesta = _post_version(datos, {**CUERPO_V1, "fecha_envio": None})

    assert respuesta.status_code == 201, respuesta.data
    assert respuesta.data["fecha_envio"] is None


def test_una_version_puede_no_traer_precios(datos):
    respuesta = _post_version(datos, {"fecha_envio": "2026-05-20"})

    assert respuesta.status_code == 201, respuesta.data
    assert respuesta.data["precios"] == []


def test_un_mes_base_mal_escrito_no_entra(datos):
    respuesta = _post_version(datos, {**CUERPO_V1, "periodo_indexacion_base": "mayo 2026"})

    assert respuesta.status_code == 400
    assert "periodo_indexacion_base" in respuesta.data


def test_un_anio_repetido_es_422(datos):
    respuesta = _post_version(datos, {
        **CUERPO_V1,
        "precios": [{"anio": 2026, "precio": 330}, {"anio": 2026, "precio": 318}],
    })

    assert respuesta.status_code == 422


def test_aceptar_una_version_por_su_numero(datos):
    _post_version(datos, CUERPO_V1)

    respuesta = _post_aceptar(datos, 1, {"fecha_aceptacion": "2026-06-01"})

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["fecha_aceptacion"] == "2026-06-01"


def test_aceptar_una_version_que_no_existe_es_404(datos):
    _post_version(datos, CUERPO_V1)

    respuesta = _post_aceptar(datos, 9, {"fecha_aceptacion": "2026-06-01"})

    assert respuesta.status_code == 404


def test_aceptar_una_segunda_version_es_409(datos):
    _post_version(datos, CUERPO_V1)
    _post_version(datos, CUERPO_V1)
    _post_aceptar(datos, 1, {"fecha_aceptacion": "2026-06-01"})

    respuesta = _post_aceptar(datos, 2, {"fecha_aceptacion": "2026-06-15"})

    assert respuesta.status_code == 409


def test_aceptar_un_borrador_es_422(datos):
    _post_version(datos, {**CUERPO_V1, "fecha_envio": None})

    respuesta = _post_aceptar(datos, 1, {"fecha_aceptacion": "2026-06-01"})

    assert respuesta.status_code == 422


def test_no_hay_como_editar_ni_borrar_una_version(datos):
    """Append-only: la ruta solo acepta GET y POST. Si alguien agrega un PATCH,
    esta prueba lo obliga a justificarlo."""
    from api.v1.comercial.views import ComercialViewSet

    metodos = ComercialViewSet.oferta_versiones.mapping.keys()

    assert set(metodos) == {"get", "post"}
