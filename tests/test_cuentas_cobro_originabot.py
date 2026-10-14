"""Cuentas de cobro de arriendos traídas de originabot (TSF) a Costos → Arriendos.

Un pago de TSF es de un propietario y una fecha, y puede cubrir varias plantas
del terreno: se guarda un `ArrDocumento` por pago y planta, con lo de esa planta.
originabot se reemplaza por un cliente falso.
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


@pytest.fixture(autouse=True)
def _limpio(tmp_path, monkeypatch):
    from apps.arriendos import models as ar
    from apps.arriendos.services import documentos
    from apps.contratos import models as ct
    from apps.proyectos.models import Proyecto

    monkeypatch.setattr(documentos, "directorio", lambda: tmp_path)

    def borrar():
        ar.ArrDocumento.objects.all().delete()
        ar.ArrArrendador.objects.all().delete()
        ct.Contrato.objects.all().delete()
        Proyecto.objects.all().delete()

    borrar()
    yield
    borrar()


PAGO_SEP = {
    "id": "2a1d6e0e-0000-4000-8000-000000000001",
    "date": "2026-09-20T00:00:00Z",
    "status": {"value": "paid", "display": "Pagado"},
    "land_lord": {"id": 596, "name": "Charlene Valentine"},
    "support_document": {"file": "https://s3.test/bucket/electronic_invoice/dian_CRU957.pdf?X-Amz=1", "signed": True},
    "rent": [
        {"project": {"id": 1191}, "amount": 1377861.0},
        {"project": {"id": 266}, "amount": 1241231.0},
        {"project": {"id": 9999}, "amount": 5.0},
    ],
    "summary": {"total_to_pay": 5226285.9},
}
PAGO_OCT_SIN_DOC = {
    "id": "2a1d6e0e-0000-4000-8000-000000000002",
    "date": "2026-10-20T00:00:00Z",
    "status": {"value": "wait_for_document", "display": "Esperando documento"},
    "land_lord": {"id": 596, "name": "Charlene Valentine"},
    "support_document": None,
    "rent": [{"project": {"id": 1191}, "amount": 1471400.0}],
    "summary": {"total_to_pay": 1699467.0},
}


class ObFalso:
    def __init__(self, token=None):
        self.descargas = []

    def minigranja(self, minifarm_id):
        return {200: {"project": {"id": 1191, "terrain_id": 369}},
                223: {"project": {"id": 266, "terrain_id": 369}}}[minifarm_id]

    def pagos_de_terreno(self, terreno, anio):
        assert (terreno, anio) == (369, 2026)
        return [PAGO_SEP, PAGO_OCT_SIN_DOC]

    def descargar(self, url):
        self.descargas.append(url)
        return b"%PDF-1.4 cuenta"


@pytest.fixture
def ob(monkeypatch):
    from apps.arriendos.services import cuentas_cobro_originabot as svc

    falso = ObFalso()
    monkeypatch.setattr(svc, "ClienteOriginabot", lambda token: falso)
    return falso


def _planta(nombre, minifarm, con_arriendo=True, estado="en_operacion"):
    from apps.arriendos import models as ar
    from apps.contratos import models as ct
    from apps.proyectos.models import Proyecto

    p = Proyecto.objects.create(
        nombre_comercial=nombre, originabot_minifarm_id=minifarm, codigo_tsf=f"COL{minifarm}", estado=estado,
    )
    if con_arriendo:
        c = ct.ContratoServicio.objects.create(servicio_aplica="arriendo", proyecto=p)
        ar.ArrArrendador.objects.create(contrato=c, nombre="Charlene Valentine", originabot_landlord_id=596)
    return p


def _svc():
    from apps.arriendos.services import cuentas_cobro_originabot

    return cuentas_cobro_originabot


def test_consulta_una_cuenta_por_pago_y_planta_del_periodo(ob):
    from apps.proyectos.models import Proyecto

    norte4 = _planta("MGS 0077 Chiriguaná Norte 4", 200)
    norte2 = _planta("MGS 0075 Chiriguaná Norte 2", 223)
    r = _svc().consultar("2026-09", "token")

    assert r["errores"] == []
    cuentas = {c["proyecto_id"]: c for c in r["cuentas"]}
    assert set(cuentas) == {norte4.id, norte2.id}
    assert cuentas[norte4.id]["valor"] == 1377861.0
    assert cuentas[norte2.id]["valor"] == 1241231.0
    assert cuentas[norte4.id]["tiene_documento"] is True
    assert cuentas[norte4.id]["tipo_documento"] == "factura_electronica"
    assert cuentas[norte4.id]["arrendador_id"] is not None
    assert "documento_url" not in cuentas[norte4.id]
    # El proyecto y el terreno de TSF quedan guardados para la próxima vez.
    assert Proyecto.objects.get(pk=norte4.id).originabot_terreno_id == 369


def test_un_pago_sin_documento_se_muestra_pero_no_se_importa(ob):
    from apps.arriendos import models as ar

    _planta("MGS 0077 Chiriguaná Norte 4", 200)
    r = _svc().consultar("2026-10", "token")
    assert [(c["estado"], c["tiene_documento"]) for c in r["cuentas"]] == [("wait_for_document", False)]

    hecho = _svc().importar("2026-10", "token", [PAGO_OCT_SIN_DOC["id"]])
    assert (hecho["importados"], hecho["sin_documento"]) == (0, 1)
    assert not ar.ArrDocumento.objects.exists()


def test_importar_baja_el_archivo_una_vez_y_guarda_un_documento_por_planta(ob, tmp_path):
    from apps.arriendos import models as ar

    norte4 = _planta("MGS 0077 Chiriguaná Norte 4", 200)
    _planta("MGS 0075 Chiriguaná Norte 2", 223)
    hecho = _svc().importar("2026-09", "token", [PAGO_SEP["id"]])

    assert hecho["importados"] == 2
    assert len(ob.descargas) == 1
    doc = ar.ArrDocumento.objects.get(proyecto_id=norte4.id)
    assert (doc.periodo, doc.originabot_pago_id, float(doc.valor_individual)) == (
        "2026-09", PAGO_SEP["id"], 1377861.0,
    )
    assert doc.nombre_arrendatario == "Charlene Valentine"
    assert open(doc.ruta_local, "rb").read() == b"%PDF-1.4 cuenta"

    # Ya importado: la consulta lo dice, y repetir no duplica.
    assert all(c["importado"] for c in _svc().consultar("2026-09", "token")["cuentas"])
    _svc().importar("2026-09", "token", [PAGO_SEP["id"]])
    assert ar.ArrDocumento.objects.count() == 2


def test_solo_las_plantas_de_la_tabla_de_arriendos(ob):
    # La tabla (`panel.calculo`) muestra las que operamos con contrato de arriendo:
    # traer cuentas de otras las dejaría sin dónde verse.
    _planta("Sin minigranja", None)
    _planta("Sin arriendo", 200, con_arriendo=False)
    _planta("En desarrollo", 223, estado="en_desarrollo")
    r = _svc().consultar("2026-09", "token")
    assert r["cuentas"] == []
    assert r["sin_minigranja"] == ["Sin minigranja"]
