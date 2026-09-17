"""`firmar()`: la oferta del CRM se convierte en su contrato PPA.

Este camino no tenía NINGUNA prueba en Django. Las que existen
(`test_comercial_pipeline_oferta.py`, `test_comercial_ficha_operativa.py`)
ejercen `app/api/v1/comercial.py`, el árbol FastAPI apagado — así que la función
que de verdad se sirve estaba sin cubrir.

Desde el 2026-09-17 `firmar()` ya no crea el contrato: se lo pide a
`apps.ppa.services.escritura.crear_ppa`, la misma que usa `POST /ppa`. Lo que se
fija acá es que el CRM siga poniendo lo suyo —de dónde salen los datos y qué le
pasa a la oferta— y que ahora herede las dos reglas que antes se saltaba:
sincronizar las partes y validar contra GESCON.

En producción este camino no se ha usado nunca: 0 de 165 ofertas tienen
`ppa_contrato_id`. Ver `docs/DIAGNOSTICO_PPA.md` §2.
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
    """Un generador con una oferta de compra de energía sobre dos plantas."""
    from django.db import transaction

    from apps.clientes.models import Cliente
    from apps.comercial.models import (
        Oportunidad, OportunidadOferta, OportunidadOfertaProyecto,
    )
    from apps.plataforma.models import Usuario
    from apps.proyectos.models import Proyecto

    atomica = transaction.atomic()
    atomica.__enter__()

    usuario = Usuario.objects.create(
        nombre="QA", email="qa@unergy.io", rol="admin", activo=True
    )
    generador = Cliente.objects.create(
        razon_social_nombre="Generador S.A.S. E.S.P.", nit_cedula="900000009-9"
    )
    plantas = [
        Proyecto.objects.create(nombre_comercial="Balmora 1"),
        Proyecto.objects.create(nombre_comercial="Balmora 2"),
    ]
    oportunidad = Oportunidad.objects.create(
        cliente_id=generador.id, nombre="Balmora", estado="oferta"
    )
    oferta = OportunidadOferta.objects.create(
        oportunidad_id=oportunidad.id, tipo="compra_energia",
        planta_nombre="Balmora 1 y 2", numero_oferta="OP.COM No.0099-9-2026",
        estado="contrato",
    )
    for p in plantas:
        OportunidadOfertaProyecto.objects.create(oferta_id=oferta.id, proyecto_id=p.id)

    yield {
        "usuario": usuario, "generador": generador, "plantas": plantas,
        "oportunidad": oportunidad, "oferta": oferta,
    }

    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


import datetime as dt  # noqa: E402

CONDICIONES = {
    "fecha_inicio": dt.date(2026, 10, 1),
    "fecha_fin": dt.date(2027, 3, 31),
    "precios_anuales": [{"anio": 2026, "precio": 300.0}, {"anio": 2027, "precio": 320.0}],
}


def _firmar(datos, condiciones=None):
    from apps.comercial.services import escritura

    return escritura.firmar(
        datos["oferta"], dict(condiciones or CONDICIONES), datos["usuario"]
    )


# ── Lo que el CRM pone ───────────────────────────────────────────────────────

def test_el_contrato_nace_de_compra_con_el_cliente_como_vendedor(datos):
    """Unergy compra la energía al generador, así que el cliente de la oferta es
    el VENDEDOR. El tipo ya no se graba a mano: sale del mapa por tipo de oferta."""
    resultado = _firmar(datos)

    contrato = resultado.contrato
    assert contrato.tipo_contrato == "compra"
    assert contrato.vendedor_id == datos["generador"].id
    assert contrato.comprador_id is None   # Unergy todavía no se escribe por llave


def test_el_codigo_y_el_nombre_salen_de_la_oferta_si_no_se_mandan(datos):
    resultado = _firmar(datos)

    assert resultado.contrato.numero_codigo_contrato == "OP.COM No.0099-9-2026"
    assert resultado.contrato.nombre_interno == "Balmora 1 y 2"


def test_se_vinculan_TODAS_las_plantas_de_la_oferta(datos):
    """Una oferta que cubre dos plantas firma un contrato con las dos; con una
    sola, Cumplimiento mediría el compromiso entero contra media planta."""
    from apps.ppa.models import PpaContratoProyecto

    resultado = _firmar(datos)

    vinculadas = set(
        PpaContratoProyecto.objects
        .filter(contrato_id=resultado.contrato.id)
        .values_list("proyecto_id", flat=True)
    )
    assert vinculadas == {p.id for p in datos["plantas"]}
    assert resultado.plantas == 2


def test_los_precios_anuales_se_expanden_recortados_al_periodo(datos):
    """Octubre 2026 a marzo 2027: seis meses, no veinticuatro. Un contrato que
    arranca en octubre no tiene tarifa de enero a septiembre de ese año."""
    from apps.ppa.models import PpaTarifa

    resultado = _firmar(datos)

    filas = list(
        PpaTarifa.objects.filter(contrato_id=resultado.contrato.id)
        .order_by("año", "mes").values_list("año", "mes")
    )
    assert filas == [(2026, 10), (2026, 11), (2026, 12), (2027, 1), (2027, 2), (2027, 3)]
    assert resultado.tarifas == 6


def test_la_oferta_queda_enlazada_y_firmada_con_historial(datos):
    from apps.comercial.models import OportunidadEstadoHistorial

    resultado = _firmar(datos)

    datos["oferta"].refresh_from_db()
    assert datos["oferta"].ppa_contrato_id == resultado.contrato.id
    assert datos["oferta"].estado == "firmado"
    assert OportunidadEstadoHistorial.objects.filter(
        oferta_id=datos["oferta"].id, estado_nuevo="firmado"
    ).exists()


def test_firmar_dos_veces_es_409(datos):
    from api.exceptions import Conflict

    _firmar(datos)

    with pytest.raises(Conflict):
        _firmar(datos)


def test_una_oferta_de_servicios_no_produce_un_ppa(datos):
    """Las de representación y CGM van a `contratos_servicio`."""
    from api.exceptions import NoProcesable

    datos["oferta"].tipo = "servicios_operacionales"
    datos["oferta"].save(update_fields=["tipo"])

    with pytest.raises(NoProcesable):
        _firmar(datos)


def test_la_comunidad_energetica_queda_marcada_en_el_contrato(datos):
    """La característica vive en el CONTRATO: si mañana se borra la oferta, el
    PPA sigue sabiendo lo que es."""
    datos["oferta"].tipo = "comunidad_energetica"
    datos["oferta"].save(update_fields=["tipo"])

    resultado = _firmar(datos)

    assert resultado.contrato.es_comunidad_energetica is True
    assert resultado.contrato.tipo_contrato == "compra"


# ── Lo que hereda de la función única ────────────────────────────────────────

def test_ahora_sincroniza_nombre_y_nit_desde_el_cliente(datos):
    """Regla que este camino NO aplicaba: la copiaba a mano y sin pasar por
    `sincronizar_partes`."""
    resultado = _firmar(datos)

    assert resultado.contrato.vendedor_nombre == "Generador S.A.S. E.S.P."
    assert resultado.contrato.vendedor_nit == "900000009-9"


def test_ahora_valida_la_fecha_de_fin_contra_gescon(datos):
    """La otra regla que se saltaba. Un registro GESCON que termina después
    dejaría la planta «vigente» más allá del contrato comercial."""
    from apps.mercado_xm.models import AsicSolicitud
    from apps.ppa.models import PpaContrato
    from apps.ppa.services.contratos import ReglaPpa

    AsicSolicitud.objects.create(
        contrato_interno="OP.COM No.0099-9-2026", fecha_fin=dt.date(2030, 12, 31),
    )
    antes = PpaContrato.objects.count()

    with pytest.raises(ReglaPpa):
        _firmar(datos)

    assert PpaContrato.objects.count() == antes, "no debió quedar contrato creado"
    datos["oferta"].refresh_from_db()
    assert datos["oferta"].ppa_contrato_id is None, "la oferta no debió quedar firmada"


def test_avisa_que_el_contrato_queda_sin_compromisos(datos):
    """`firmar()` no crea compromisos de energía —nunca los creó— y sin ellos
    Cumplimiento no tiene mínimo contra el cual medir. Ahora al menos se dice."""
    resultado = _firmar(datos)

    assert resultado.compromisos == 0
    assert any("compromisos" in a.lower() for a in resultado.avisos)
    assert not any("plantas" in a.lower() for a in resultado.avisos)
