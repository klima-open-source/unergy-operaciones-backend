"""La fusión de clientes no deja nada apuntando a la ficha que se borra.

Hasta el 2026-10-07 se le escapaban tres tablas: el arrendador de un arriendo, el
inversionista de una factura y las tasas de IVA/retención por servicio. Quedaban
conectadas a la ficha borrada, sin error. La primera prueba vigila la lista contra
los modelos: una tabla nueva con FK a `clientes` tiene que entrar a la fusión.
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
def datos(monkeypatch):
    from django.db import connection, transaction

    from apps.clientes.models import Cliente
    from apps.clientes.services import gestion

    # La fusión está escrita para Postgres: `audit_log` no es un modelo de Django
    # (en SQLite no existe) y el soft-delete usa `NOW()`.
    monkeypatch.setattr(gestion, "registrar_borrado", lambda *a, **kw: None)
    atomica = transaction.atomic()
    atomica.__enter__()
    connection.connection.create_function("NOW", 0, lambda: "2026-10-07 00:00:00")
    yield {
        "ganador": Cliente.objects.create(razon_social_nombre="Bia Energy S.A.S."),
        "perdedor": Cliente.objects.create(razon_social_nombre="Bia Energy"),
    }
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _fusionar(ganador, perdedor):
    from apps.clientes.services import gestion

    movimientos, copiados = gestion.reporte_merge(ganador, perdedor)
    gestion.ejecutar_merge(ganador, perdedor, movimientos, copiados)
    return {m["tabla"]: m for m in movimientos}


def test_toda_tabla_que_apunta_a_un_cliente_entra_a_la_fusion():
    from django.apps import apps as django_apps

    from apps.clientes.models import Cliente
    from apps.clientes.services import gestion

    cubiertas = (
        set(gestion.MERGE_SIMPLE)
        | {(t, "cliente_id") for t, _ in gestion.MERGE_COMPUESTO}
        | {("contratos", c) for c in gestion.CAMPOS_PARTE_CONTRATO}
    )
    a_proposito_fuera = {("email_envios", "cliente_id")}  # un log de correos

    faltan = set()
    for modelo in django_apps.get_models():
        if modelo._meta.proxy:
            continue
        for campo in modelo._meta.concrete_fields:
            if campo.is_relation and campo.related_model is Cliente:
                clave = (modelo._meta.db_table, campo.column)
                if clave not in cubiertas | a_proposito_fuera:
                    faltan.add(clave)
    assert not faltan, f"Estas FK a clientes no las mueve la fusión: {sorted(faltan)}"


def test_el_arrendador_pasa_a_la_ficha_que_queda(datos):
    from apps.arriendos.models import ArrArrendador
    from apps.contratos.models import ContratoServicio

    contrato = ContratoServicio.objects.create(servicio_aplica="arriendo")
    arrendador = ArrArrendador.objects.create(
        contrato=contrato, nombre="Bia Energy", cliente=datos["perdedor"],
    )
    movimientos = _fusionar(datos["ganador"], datos["perdedor"])

    arrendador.refresh_from_db()
    assert arrendador.cliente_id == datos["ganador"].id
    assert movimientos["arr_arrendador"]["a_mover"] == 1


def test_la_factura_pasa_a_la_ficha_que_queda(datos):
    from apps.contratos.models import ContratoServicio
    from apps.facturacion.models import ContratoFactura

    contrato = ContratoServicio.objects.create(servicio_aplica="representacion")
    factura = ContratoFactura.objects.create(
        contrato=contrato, tipo="inversionista", fecha="2026-09",
        inversionista=datos["perdedor"], inversionista_nombre="Bia Energy",
    )
    _fusionar(datos["ganador"], datos["perdedor"])

    factura.refresh_from_db()
    assert factura.inversionista_id == datos["ganador"].id
    assert factura.inversionista_nombre == "Bia Energy", "el texto es el de la emisión"


def test_las_tasas_pasan_y_si_chocan_gana_la_del_ganador(datos):
    from apps.clientes.models import ClienteTasaServicio

    # Las dos fichas tienen tasa general (sin planta) para representación: choca.
    ClienteTasaServicio.objects.create(cliente=datos["ganador"], servicio="representacion",
                                       iva_pct=19)
    ClienteTasaServicio.objects.create(cliente=datos["perdedor"], servicio="representacion",
                                       iva_pct=0)
    # Solo el perdedor tiene tasa para CGM: pasa.
    ClienteTasaServicio.objects.create(cliente=datos["perdedor"], servicio="cgm", iva_pct=5)

    movimientos = _fusionar(datos["ganador"], datos["perdedor"])

    tasas = sorted(ClienteTasaServicio.objects.filter(cliente=datos["ganador"])
                   .values_list("servicio", "iva_pct"))
    assert [(s, int(i)) for s, i in tasas] == [("cgm", 5), ("representacion", 19)]
    assert not ClienteTasaServicio.objects.filter(cliente=datos["perdedor"]).exists()
    assert movimientos["cliente_tasa_servicio"] == {
        "tabla": "cliente_tasa_servicio", "a_mover": 1, "descartadas_por_colision": 1,
    }
