"""Las partes de cada contrato: la tabla `contrato_partes` y quién la escribe.

La escribe SOLO `apps/contratos/services/contrato_partes.py`, desde `Contrato.save()`,
derivándola de las columnas comprador/vendedor/contratante/prestador. Los dos caminos
que no pasan por `save()` se ocupan de ella por su cuenta: la fusión de clientes y el
comando `registrar_partes_contratos`.
"""
from io import StringIO
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

    from apps.clientes.models import Cliente

    atomica = transaction.atomic()
    atomica.__enter__()
    yield {
        "unergy": Cliente.objects.create(razon_social_nombre="Unergy"),
        "solenium": Cliente.objects.create(razon_social_nombre="Solenium"),
        "bia": Cliente.objects.create(razon_social_nombre="Bia Energy"),
    }
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _partes(contrato):
    from apps.contratos.models import ContratoParte

    return sorted(ContratoParte.objects.filter(contrato_id=contrato.pk)
                  .values_list("rol", "cliente_id"))


# ── La regla (pura) ──────────────────────────────────────────────────────────


def test_las_partes_salen_de_las_columnas_con_cliente():
    from apps.contratos.services import contrato_partes

    c = SimpleNamespace(comprador_id=None, vendedor_id=None, contratante_id=7, prestador_id=3)
    assert contrato_partes.deseadas(c) == {("contratante", 7), ("prestador", 3)}


def test_el_inversionista_no_es_una_parte():
    from apps.contratos.services import contrato_partes

    c = SimpleNamespace(comprador_id=None, vendedor_id=None, contratante_id=None,
                        prestador_id=None, inversionista_id=9)
    assert contrato_partes.deseadas(c) == set()


# ── Contrato.save() la mantiene al día ───────────────────────────────────────


def test_guardar_un_contrato_de_servicio_registra_sus_partes(datos):
    from apps.contratos.models import ContratoServicio

    c = ContratoServicio.objects.create(
        servicio_aplica="mantenimiento",
        contratante_id=datos["unergy"].id, prestador_id=datos["solenium"].id,
    )
    assert _partes(c) == [("contratante", datos["unergy"].id),
                          ("prestador", datos["solenium"].id)]


def test_guardar_un_ppa_registra_comprador_y_vendedor(datos):
    from apps.ppa.models import PpaContrato

    ppa = PpaContrato.objects.create(
        tipo_contrato="venta", nombre_interno="PPA",
        vendedor_id=datos["unergy"].id, comprador_id=datos["bia"].id,
    )
    assert _partes(ppa) == [("comprador", datos["bia"].id), ("vendedor", datos["unergy"].id)]


def test_cambiar_o_quitar_una_parte_actualiza_la_tabla(datos):
    from apps.contratos.models import ContratoServicio

    c = ContratoServicio.objects.create(
        servicio_aplica="representacion",
        contratante_id=datos["bia"].id, prestador_id=datos["unergy"].id,
    )
    c.contratante_id = datos["solenium"].id
    c.prestador_id = None
    c.save()
    assert _partes(c) == [("contratante", datos["solenium"].id)]


def test_un_cliente_con_dos_papeles_tiene_dos_filas_y_sale_una_vez(datos):
    from apps.contratos.models import ContratoServicio
    from apps.contratos.services import contrato_partes

    c = ContratoServicio.objects.create(
        servicio_aplica="representacion",
        contratante_id=datos["unergy"].id, prestador_id=datos["unergy"].id,
    )
    assert len(_partes(c)) == 2
    ids = ContratoServicio.objects.filter(pk__in=contrato_partes.contratos_de({datos["unergy"].id}))
    assert list(ids.values_list("id", flat=True)) == [c.id]


def test_contratos_de_filtra_por_papel(datos):
    from apps.contratos.models import Contrato, ContratoServicio
    from apps.contratos.services import contrato_partes
    from apps.ppa.models import PpaContrato

    serv = ContratoServicio.objects.create(
        servicio_aplica="mantenimiento", contratante_id=datos["unergy"].id,
    )
    ppa = PpaContrato.objects.create(
        tipo_contrato="venta", nombre_interno="PPA", vendedor_id=datos["unergy"].id,
    )
    unergy = {datos["unergy"].id}
    todos = set(Contrato.objects.filter(pk__in=contrato_partes.contratos_de(unergy))
                .values_list("id", flat=True))
    solo_ppa = set(Contrato.objects.filter(
        pk__in=contrato_partes.contratos_de(unergy, contrato_partes.ROLES_PPA)
    ).values_list("id", flat=True))
    assert todos == {serv.id, ppa.id}
    assert solo_ppa == {ppa.id}


# ── El comando que la llena y la verifica ────────────────────────────────────


def test_el_comando_llena_lo_que_falta_y_despues_cuadra(datos):
    from django.core.management import call_command

    from apps.contratos.models import ContratoParte, ContratoServicio

    c = ContratoServicio.objects.create(
        servicio_aplica="arriendo",
        contratante_id=datos["unergy"].id, prestador_id=datos["bia"].id,
    )
    ContratoParte.objects.all().delete()  # como queda la tabla justo tras la migración

    salida = StringIO()
    call_command("registrar_partes_contratos", stdout=salida)
    assert "Filas que faltan en la tabla    : 2" in salida.getvalue()
    assert _partes(c) == [], "sin --aplicar no escribe"

    call_command("registrar_partes_contratos", "--aplicar", stdout=StringIO())
    assert len(_partes(c)) == 2

    salida = StringIO()
    call_command("registrar_partes_contratos", stdout=salida)
    assert "La tabla cuadra con las columnas." in salida.getvalue()


# ── La fusión de clientes las mueve ──────────────────────────────────────────


@pytest.fixture
def sin_auditoria(monkeypatch):
    """La fusión está escrita para Postgres: `audit_log` no es un modelo de Django
    (en SQLite no existe) y el soft-delete usa `NOW()`."""
    from django.db import connection

    from apps.clientes.services import gestion

    monkeypatch.setattr(gestion, "registrar_borrado", lambda *a, **kw: None)
    connection.ensure_connection()
    connection.connection.create_function("NOW", 0, lambda: "2026-10-07 00:00:00")


def test_fusionar_clientes_mueve_sus_partes(datos, sin_auditoria):
    from apps.clientes.models import Cliente
    from apps.clientes.services import gestion
    from apps.contratos.models import ContratoServicio

    duplicado = Cliente.objects.create(razon_social_nombre="SOLENIUM S.A.S.")
    c = ContratoServicio.objects.create(
        servicio_aplica="mantenimiento",
        contratante_id=datos["unergy"].id, prestador_id=duplicado.id,
    )

    movimientos, copiados = gestion.reporte_merge(datos["solenium"], duplicado)
    assert {"tabla": "contrato_partes", "a_mover": 1, "descartadas_por_colision": 0} in movimientos
    gestion.ejecutar_merge(datos["solenium"], duplicado, movimientos, copiados)

    c.refresh_from_db()
    assert c.prestador_id == datos["solenium"].id
    assert _partes(c) == [("contratante", datos["unergy"].id),
                          ("prestador", datos["solenium"].id)]


def test_fusionar_cuando_el_ganador_ya_era_esa_parte_no_duplica(datos, sin_auditoria):
    from apps.clientes.models import Cliente
    from apps.clientes.services import gestion
    from apps.contratos.models import ContratoParte, ContratoServicio

    duplicado = Cliente.objects.create(razon_social_nombre="UNERGY S.A.S.")
    c = ContratoServicio.objects.create(
        servicio_aplica="representacion", prestador_id=datos["unergy"].id,
    )
    # Dos dueños del mismo papel: el caso de un terreno con varios arrendadores.
    ContratoParte.objects.create(contrato_id=c.pk, rol="prestador", cliente_id=duplicado.id)

    movimientos, copiados = gestion.reporte_merge(datos["unergy"], duplicado)
    gestion.ejecutar_merge(datos["unergy"], duplicado, movimientos, copiados)

    assert _partes(c) == [("prestador", datos["unergy"].id)]
