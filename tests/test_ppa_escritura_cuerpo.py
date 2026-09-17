"""El CUERPO de todas las escrituras de `/ppa`, no solo sus rutas.

`test_paridad_urls.py` compara las tablas de rutas de los dos backends y no mira
lo que viaja adentro. Entre el port a Django (2026-09-04) y el 2026-09-17,
`POST /ppa` aceptó el cuerpo del frontend, respondió **201** y descartó en
silencio `comprador_id`, `vendedor_id` y `responsable_id`: el serializer los
declaraba con el nombre del ORM (`comprador`, `vendedor`, `responsable`) y DRF
ignora las claves que no reconoce. El contrato quedaba sin partes y sin
responsable, y nadie lo veía porque ninguna prueba mandaba un cuerpo.

Estas pruebas fijan el contrato con el nombre que la LECTURA ya devolvía y que
`PPAContratoCreate` de FastAPI recibía, y de paso cubren el resto de las
escrituras del recurso —tarifas, compromisos, el borrado y los responsables—,
que tampoco tenían ninguna. Ver `docs/DIAGNOSTICO_PPA.md` §3.

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


# ── Tarifas y compromisos: las dos series REEMPLAZAN ─────────────────────────
#
# El `PUT` borra y reinserta en vez de hacer upsert por período, para que un mes
# que el usuario quitó en pantalla también desaparezca de la base. El precio de
# esa decisión es que un cuerpo incompleto borra lo que no viene, y eso no
# estaba cubierto por ninguna prueba.

def _poner_tarifas(datos, pk, filas):
    return _peticion(
        datos, "put", f"/api/v1/ppa/{pk}/tarifas", filas, "tarifas", pk=str(pk)
    )


def _poner_compromisos(datos, pk, filas):
    return _peticion(
        datos, "put", f"/api/v1/ppa/{pk}/compromisos", filas, "compromisos", pk=str(pk)
    )


def test_las_tarifas_se_guardan_y_vuelven_ordenadas(datos):
    pk = _crear(datos, CUERPO_MINIMO).data["id"]

    respuesta = _poner_tarifas(datos, pk, [
        {"año": 2026, "mes": 3, "tarifa": 310.5},
        {"año": 2026, "mes": 1, "tarifa": 300},
    ])

    assert respuesta.status_code == 200, respuesta.data
    assert [(f["año"], f["mes"]) for f in respuesta.data] == [(2026, 1), (2026, 3)]


def test_un_segundo_put_de_tarifas_reemplaza_todo(datos):
    """Lo que no viene en el cuerpo se borra. Es a propósito, y por eso se fija."""
    pk = _crear(datos, CUERPO_MINIMO).data["id"]
    _poner_tarifas(datos, pk, [
        {"año": 2026, "mes": 1, "tarifa": 300},
        {"año": 2026, "mes": 2, "tarifa": 300},
    ])

    respuesta = _poner_tarifas(datos, pk, [{"año": 2026, "mes": 2, "tarifa": 320}])

    assert [(f["año"], f["mes"]) for f in respuesta.data] == [(2026, 2)]


def test_un_put_de_tarifas_vacio_deja_el_contrato_sin_tarifas(datos):
    """Mandar una lista vacía borra la tabla entera del contrato: el front no
    debe llamar a esto cuando el usuario simplemente no cargó tarifas."""
    from apps.ppa.models import PpaTarifa

    pk = _crear(datos, CUERPO_MINIMO).data["id"]
    _poner_tarifas(datos, pk, [{"año": 2026, "mes": 1, "tarifa": 300}])

    respuesta = _poner_tarifas(datos, pk, [])

    assert respuesta.status_code == 200
    assert respuesta.data == []
    assert PpaTarifa.objects.filter(contrato_id=pk).count() == 0


def test_un_mes_fuera_de_rango_no_entra(datos):
    pk = _crear(datos, CUERPO_MINIMO).data["id"]

    respuesta = _poner_tarifas(datos, pk, [{"año": 2026, "mes": 13, "tarifa": 300}])

    assert respuesta.status_code == 400


def test_las_tarifas_de_un_contrato_que_no_existe_son_404(datos):
    respuesta = _poner_tarifas(datos, 10**9, [{"año": 2026, "mes": 1, "tarifa": 300}])

    assert respuesta.status_code == 404


def test_los_compromisos_se_guardan_con_su_maximo_y_su_conteo(datos):
    pk = _crear(datos, CUERPO_MINIMO).data["id"]

    respuesta = _poner_compromisos(datos, pk, [
        {"año": 2026, "mes": 1, "energia_minima": 100, "energia_maxima": 150,
         "cantidad_proyectos": 3},
    ])

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data[0]["cantidad_proyectos"] == 3


def test_el_maximo_y_el_conteo_son_opcionales(datos):
    """El wizard los marca opcionales: un contrato puede tener solo mínimo."""
    pk = _crear(datos, CUERPO_MINIMO).data["id"]

    respuesta = _poner_compromisos(datos, pk, [
        {"año": 2026, "mes": 1, "energia_minima": 100},
    ])

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data[0]["energia_maxima"] is None


def test_un_segundo_put_de_compromisos_reemplaza_todo(datos):
    pk = _crear(datos, CUERPO_MINIMO).data["id"]
    _poner_compromisos(datos, pk, [
        {"año": 2026, "mes": 1, "energia_minima": 100},
        {"año": 2026, "mes": 2, "energia_minima": 100},
    ])

    respuesta = _poner_compromisos(datos, pk, [
        {"año": 2026, "mes": 2, "energia_minima": 120},
    ])

    assert [(f["año"], f["mes"]) for f in respuesta.data] == [(2026, 2)]


# ── El borrado es LÓGICO, y se bloquea si algo cuelga ────────────────────────

def _borrar(datos, pk):
    return _peticion(datos, "delete", f"/api/v1/ppa/{pk}", None, "destroy", pk=str(pk))


def test_borrar_no_saca_la_fila_de_la_base(datos):
    """Marca `deleted_at` en vez de borrar: el contrato deja de listarse pero el
    rastro queda, y de él cuelgan registros de otros dominios."""
    from apps.ppa.models import PpaContrato

    pk = _crear(datos, CUERPO_MINIMO).data["id"]

    respuesta = _borrar(datos, pk)

    assert respuesta.status_code == 204
    assert PpaContrato.objects.get(pk=pk).deleted_at is not None
    assert not PpaContrato.objects.filter(pk=pk, deleted_at__isnull=True).exists()


def test_no_se_borra_un_contrato_con_registros_gescon(datos):
    """El registro ante XM es lo que conecta el contrato con el despacho real:
    borrarlo dejaría esos registros apuntando a un contrato invisible."""
    from apps.mercado_xm.models import AsicSolicitud

    pk = _crear(datos, CUERPO_MINIMO).data["id"]
    AsicSolicitud.objects.create(contrato_ppa_id=pk, contrato_interno="UNERGY-TEST-001")

    respuesta = _borrar(datos, pk)

    assert respuesta.status_code == 409
    assert "GESCON" in str(respuesta.data)


def test_no_se_borra_un_contrato_con_cumplimiento_cerrado(datos):
    from apps.mercado_xm.models import CumplimientoMensual

    pk = _crear(datos, CUERPO_MINIMO).data["id"]
    CumplimientoMensual.objects.create(contrato_ppa_id=pk, anio=2026, mes=1)

    respuesta = _borrar(datos, pk)

    assert respuesta.status_code == 409
    assert "cumplimiento" in str(respuesta.data).lower()


# ── Responsables: el catálogo que decide quién entra a la matriz ─────────────

def _crear_responsable(datos, cuerpo):
    return _peticion(datos, "post", "/api/v1/ppa/responsables", cuerpo, "responsables")


def _editar_responsable(datos, rid, cuerpo):
    return _peticion(
        datos, "patch", f"/api/v1/ppa/responsables/{rid}", cuerpo, "responsable",
        rid=str(rid),
    )


def _borrar_responsable(datos, rid):
    return _peticion(
        datos, "delete", f"/api/v1/ppa/responsables/{rid}", None, "responsable",
        rid=str(rid),
    )


def _asignar(datos, cuerpo):
    return _peticion(
        datos, "post", "/api/v1/ppa/responsables/asignar", cuerpo,
        "asignar_responsable",
    )


def test_crear_responsable(datos):
    respuesta = _crear_responsable(
        datos, {"nombre": "  Tercero S.A.S.  ", "incluir_en_cumplimiento": False}
    )

    assert respuesta.status_code == 201, respuesta.data
    assert respuesta.data["nombre"] == "Tercero S.A.S."   # se recorta
    assert respuesta.data["incluir_en_cumplimiento"] is False
    assert respuesta.data["n_contratos"] == 0


def test_un_responsable_con_nombre_repetido_es_409(datos):
    """Sin esto el catálogo se llena de variantes del mismo tercero y los
    filtros de la matriz dejan de agrupar."""
    respuesta = _crear_responsable(datos, {"nombre": "unergy pruebas"})

    assert respuesta.status_code == 409


def test_un_nombre_vacio_no_crea_responsable(datos):
    respuesta = _crear_responsable(datos, {"nombre": "   "})

    assert respuesta.status_code in (400, 422)


def test_editar_responsable_cambia_nombre_y_bandera(datos):
    rid = datos["responsable"].id

    respuesta = _editar_responsable(
        datos, rid, {"nombre": "Unergy Renombrado", "incluir_en_cumplimiento": False}
    )

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["nombre"] == "Unergy Renombrado"
    assert respuesta.data["incluir_en_cumplimiento"] is False


def test_editar_responsable_no_choca_consigo_mismo(datos):
    """Guardar sin cambiar el nombre no puede dar 409."""
    rid = datos["responsable"].id

    respuesta = _editar_responsable(datos, rid, {"nombre": "Unergy Pruebas"})

    assert respuesta.status_code == 200, respuesta.data


def test_no_se_borra_un_responsable_con_contratos(datos):
    """Reasignarlos primero es explícito; dejarlos en null los haría reaparecer
    en la matriz de cumplimiento sin que nadie se entere."""
    _crear(datos, {**CUERPO_MINIMO, "responsable_id": datos["responsable"].id})

    respuesta = _borrar_responsable(datos, datos["responsable"].id)

    assert respuesta.status_code == 409


def test_se_borra_un_responsable_sin_contratos(datos):
    from apps.ppa.models import PpaResponsable

    rid = _crear_responsable(datos, {"nombre": "Efímero"}).data["id"]

    respuesta = _borrar_responsable(datos, rid)

    assert respuesta.status_code == 204
    assert not PpaResponsable.objects.filter(pk=rid).exists()


def test_asignar_responsable_a_varios_contratos(datos):
    from apps.ppa.models import PpaContrato

    uno = _crear(datos, CUERPO_MINIMO).data["id"]
    otro = _crear(datos, {**CUERPO_MINIMO, "numero_codigo_contrato": "T-2"}).data["id"]

    respuesta = _asignar(datos, {
        "contrato_ids": [uno, otro], "responsable_id": datos["responsable"].id,
    })

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["actualizados"] == 2
    assert PpaContrato.objects.get(pk=uno).responsable_id == datos["responsable"].id


def test_asignar_con_null_desasigna(datos):
    from apps.ppa.models import PpaContrato

    pk = _crear(
        datos, {**CUERPO_MINIMO, "responsable_id": datos["responsable"].id}
    ).data["id"]

    respuesta = _asignar(datos, {"contrato_ids": [pk], "responsable_id": None})

    assert respuesta.status_code == 200, respuesta.data
    assert PpaContrato.objects.get(pk=pk).responsable_id is None


def test_asignar_un_responsable_que_no_existe_es_404(datos):
    pk = _crear(datos, CUERPO_MINIMO).data["id"]

    respuesta = _asignar(datos, {"contrato_ids": [pk], "responsable_id": 10**9})

    assert respuesta.status_code == 404


def test_asignar_no_toca_los_contratos_borrados(datos):
    """`asignar` filtra por los vivos: un contrato archivado no se revive por
    estar en la lista."""
    pk = _crear(datos, CUERPO_MINIMO).data["id"]
    _borrar(datos, pk)

    respuesta = _asignar(datos, {
        "contrato_ids": [pk], "responsable_id": datos["responsable"].id,
    })

    assert respuesta.data["actualizados"] == 0
