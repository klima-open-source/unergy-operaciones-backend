"""`vincular_partes_contratos`: el backfill que permite retirar la adivinanza.

Mientras las partes de los contratos viejos sigan sin `*_id`, cada cálculo que
necesita saber de quién es un contrato tiene que emparejar por nombre, y
`partes.sincronizar` tiene que resolver al vuelo en cada guardado. Este comando
escribe el vínculo una vez.

Lo que estas pruebas fijan: que **sin `--aplicar` no escribe nada** (es un
comando que toca producción), que no pisa un vínculo ya puesto, y que una parte
sin cliente que empareje se reporta en vez de inventarse uno.
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
def entorno():
    from django.db import transaction

    atomica = transaction.atomic()
    atomica.__enter__()
    yield
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _correr(*args):
    from io import StringIO

    from django.core.management import call_command

    salida = StringIO()
    call_command("vincular_partes_contratos", *args, stdout=salida)
    return salida.getvalue()


def _cliente(nombre, nit=None):
    from apps.clientes.models import Cliente

    return Cliente.objects.create(razon_social_nombre=nombre, nit_cedula=nit)


def _contrato(**kw):
    from apps.contratos.models import ContratoServicio

    kw.setdefault("servicio_aplica", "representacion")
    return ContratoServicio.objects.create(**kw)


def test_sin_aplicar_no_escribe_nada(entorno):
    """El comando toca producción: por defecto solo informa."""
    from apps.contratos.models import ContratoServicio

    _cliente("Quantum Energy Ingenieria S.A.S.", "900111222-3")
    contrato = _contrato(contratante_nombre="Quantum Energy Ingenieria S.A.S.")

    salida = _correr()

    assert ContratoServicio.objects.get(pk=contrato.id).contratante_id is None
    assert "no se escribió nada" in salida


def test_aplicar_vincula_por_nombre(entorno):
    from apps.contratos.models import ContratoServicio

    cliente = _cliente("Quantum Energy Ingenieria S.A.S.", "900111222-3")
    contrato = _contrato(contratante_nombre="Quantum Energy Ingenieria S.A.S.")

    _correr("--aplicar")

    assert ContratoServicio.objects.get(pk=contrato.id).contratante_id == cliente.id


def test_aplicar_vincula_el_inversionista(entorno):
    """El que alimenta el reparto de costos, y el único sin columna de NIT."""
    from apps.contratos.models import ContratoServicio

    cliente = _cliente("Fondo Solar Uno S.A.S.", "902555666-7")
    contrato = _contrato(inversionista_nombre="Fondo Solar Uno S.A.S.")

    _correr("--aplicar")

    assert ContratoServicio.objects.get(pk=contrato.id).inversionista_id == cliente.id


def test_no_pisa_un_vinculo_ya_puesto(entorno):
    """Corregir un vínculo existente es decisión de alguien, no de un
    emparejamiento por texto."""
    from apps.contratos.models import ContratoServicio

    puesto = _cliente("El Vinculado A Mano")
    _cliente("Otra Empresa S.A.S.", "900999888-7")
    contrato = _contrato(contratante=puesto, contratante_nombre="Otra Empresa S.A.S.")

    _correr("--aplicar")

    assert ContratoServicio.objects.get(pk=contrato.id).contratante_id == puesto.id


def test_sin_candidato_se_reporta_y_no_inventa_cliente(entorno):
    from apps.clientes.models import Cliente
    from apps.contratos.models import ContratoServicio

    _cliente("Nada Que Ver S.A.S.")
    contrato = _contrato(contratante_nombre="Empresa Inexistente Del Valle")
    cuantos = Cliente.objects.count()

    salida = _correr("--aplicar")

    assert ContratoServicio.objects.get(pk=contrato.id).contratante_id is None
    assert Cliente.objects.count() == cuantos, "no debe crear clientes"
    assert "Sin cliente que empareje        : 1" in salida


def test_tambien_recorre_las_partes_del_ppa(entorno):
    from apps.ppa.models import PpaContrato

    vendedor = _cliente("Generadora del Cauca S.A.S.", "901222333-4")
    contrato = PpaContrato.objects.create(
        vendedor_nombre="Generadora del Cauca S.A.S.", tipo_contrato="compra",
    )

    _correr("--aplicar")

    assert PpaContrato.objects.get(pk=contrato.id).vendedor_id == vendedor.id


# ── Solo lo seguro se escribe (casos reales del 2026-10-07) ─────────────────


def test_una_palabra_generica_en_comun_no_es_parecido(entorno):
    """ "Bia Energy" casaba con "BALI ENERGY" (cinco PPA) por la palabra "energy"."""
    from apps.ppa.models import PpaContrato

    _cliente("BALI ENERGY S.A.S.")
    _cliente("CSCI COLOMBIA SOLAR CORP")
    bia = PpaContrato.objects.create(comprador_nombre=" Bia Energy S.A.S.",
                                     comprador_nit="901588412", tipo_contrato="venta")
    nitro = PpaContrato.objects.create(comprador_nombre="NITRO ENERGY COLOMBIA S A S E S P",
                                       tipo_contrato="venta")

    salida = _correr("--aplicar")

    for ppa in (bia, nitro):
        ppa.refresh_from_db()
        assert ppa.comprador_id is None
    assert "Sin cliente que empareje        : 2" in salida


def test_lo_parecido_se_lista_para_revisar_y_no_se_escribe(entorno):
    """Una persona y la empresa de su familia comparten apellidos: puede ser o no."""
    from apps.arriendos.models import ArrArrendador

    empresa = _cliente("INVERSIONES ESTRADA ARBELAEZ Y CIA S. EN C")
    arrendador = ArrArrendador.objects.create(
        contrato=_contrato(servicio_aplica="arriendo"), nombre="Mauricio Estrada Arbelaez")

    salida = _correr("--aplicar")

    arrendador.refresh_from_db()
    assert arrendador.cliente_id is None
    assert "Parecidas, a revisar a mano     : 1" in salida
    assert f"(cliente {empresa.id})" in salida and "Mauricio Estrada Arbelaez" in salida


def test_el_mismo_nombre_en_otro_orden_si_se_escribe(entorno):
    from apps.arriendos.models import ArrArrendador

    cliente = _cliente("RODRIGUEZ VELEZ BEATRIZ")
    arrendador = ArrArrendador.objects.create(
        contrato=_contrato(servicio_aplica="arriendo"), nombre="Beatriz Rodriguez Velez")

    _correr("--aplicar")

    arrendador.refresh_from_db()
    assert arrendador.cliente_id == cliente.id


def test_por_nit_se_escribe_aunque_el_nombre_difiera(entorno):
    from apps.contratos.models import ContratoServicio

    cliente = _cliente("Razon Social Nueva S.A.S.", "900111222-3")
    contrato = _contrato(contratante_nombre="Nombre Viejo", contratante_nit="900.111.222-3")

    _correr("--aplicar")

    assert ContratoServicio.objects.get(pk=contrato.id).contratante_id == cliente.id


def test_al_guardar_un_contrato_tampoco_casa_por_una_palabra_generica(entorno):
    """`resolver_cliente_id` es la red de `partes.sincronizar` en cada guardado:
    la misma exclusión. El parecido con palabras propias sí lo sugiere ahí."""
    from apps.contratos.services import partes

    _cliente("BALI ENERGY S.A.S.")
    empresa = _cliente("INVERSIONES ESTRADA ARBELAEZ Y CIA S. EN C")

    assert partes.resolver_cliente_id("Bia Energy S.A.S.", None) is None
    assert partes.emparejar_cliente("Mauricio Estrada Arbelaez", None) == (
        empresa.id, partes.PARECIDO)
