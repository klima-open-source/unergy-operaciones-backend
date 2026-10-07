"""Las partes de cada contrato: la tabla `contrato_partes` y quién la escribe.

Las partes se escriben asignando en el contrato (`contrato.contratante_id = 5`) y
guardándolo: `Contrato.save()` las pasa a `contrato_partes`. El nombre y el NIT NO se
guardan en el contrato: son los de la ficha del cliente, y escribirlos da error. La
fusión de clientes, que no pasa por `save()`, mueve la tabla por su cuenta.
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


# ── Nombre y NIT salen de la ficha ───────────────────────────────────────────


def test_nombre_y_nit_son_los_de_la_ficha(datos):
    from apps.contratos.models import ContratoServicio

    datos["solenium"].nit_cedula = "9010972445"
    datos["solenium"].save()
    c = ContratoServicio.objects.create(
        servicio_aplica="mantenimiento", prestador_id=datos["solenium"].id,
    )
    c = ContratoServicio.objects.get(pk=c.pk)
    assert c.prestador_nombre == "Solenium"
    assert c.prestador_nit == "9010972445"
    assert c.contratante_nombre is None


def test_el_nombre_y_el_nit_no_se_escriben_en_el_contrato(datos):
    from apps.contratos.models import ContratoServicio

    c = ContratoServicio(servicio_aplica="mantenimiento")
    with pytest.raises(AttributeError):
        c.prestador_nombre = "Texto suelto"
    with pytest.raises(AttributeError):
        c.prestador_nit = "900"


def test_el_inversionista_no_es_una_parte(datos):
    from apps.contratos.models import ContratoServicio

    c = ContratoServicio.objects.create(
        servicio_aplica="representacion", inversionista_id=datos["bia"].id,
    )
    assert _partes(c) == []


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


# ── Escrituras parciales y listados ──────────────────────────────────────────


def test_guardar_solo_la_parte_con_update_fields(datos):
    from apps.contratos.models import ContratoServicio

    c = ContratoServicio.objects.create(servicio_aplica="mantenimiento")
    c.prestador_id = datos["solenium"].id
    c.save(update_fields=["prestador_id"])
    assert _partes(c) == [("prestador", datos["solenium"].id)]


def test_un_listado_precargado_no_consulta_por_contrato(datos):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.contratos.models import ContratoServicio
    from apps.contratos.services import contrato_partes

    for _ in range(5):
        ContratoServicio.objects.create(
            servicio_aplica="mantenimiento",
            contratante_id=datos["unergy"].id, prestador_id=datos["solenium"].id,
        )
    with CaptureQueriesContext(connection) as consultas:
        nombres = [
            (c.contratante_nombre, c.prestador_nombre)
            for c in ContratoServicio.objects.prefetch_related(contrato_partes.CON_PARTES)
        ]
    assert nombres == [("Unergy", "Solenium")] * 5
    assert len(consultas) == 2, "el contrato y sus partes, no una consulta por contrato"


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
