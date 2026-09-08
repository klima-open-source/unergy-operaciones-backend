"""GET /proyectos (arbol Django, el que de verdad corre en produccion) --
ppa_id/sin_ppa, que el port a Django dejo sin traer.

FastAPI (app/api/v1/proyectos.py) ya tenia estos filtros -- incluido el fix de
borrado logico de PpaContrato -- pero nadie los porto a Django/DRF
(api/v1/proyectos/views.py). Django reemplazo a FastAPI en produccion el
2026-09-04 (ver Dockerfile: `CMD gunicorn config.wsgi:application`), asi que el
filtro nunca existio para nadie pese a que `tests/test_proyectos_filtro_ppa.py`
pasaba en verde. Mismo patron de bug que documenta `tests/test_fallas_django.py`.

No hay `pytest-django`: el fixture arma una base sqlite en memoria con
`create_test_db` y `MIGRATION_MODULES` a `None`, igual que test_fallas_django.py.
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


def _pedir(metodo, url, datos_usuario, **kwargs):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.proyectos.views import ProyectoViewSet

    factory = APIRequestFactory()
    peticion = getattr(factory, metodo)(url)
    force_authenticate(peticion, user=UsuarioAutenticado(datos_usuario["usuario"]))
    vista = ProyectoViewSet.as_view(kwargs.pop("acciones"))
    return vista(peticion, **kwargs)


def _proyecto(**kw):
    from apps.proyectos.models import Proyecto

    kw.setdefault("nombre_comercial", "Proyecto")
    return Proyecto.objects.create(**kw)


def _contrato(**kw):
    from apps.ppa.models import PpaContrato

    return PpaContrato.objects.create(**kw)


def _vincular(proyecto, contrato):
    from apps.ppa.models import PpaContratoProyecto

    PpaContratoProyecto.objects.create(contrato=contrato, proyecto=proyecto)


def _listar(datos, query=""):
    respuesta = _pedir("get", f"/api/v1/proyectos{query}", datos, acciones={"get": "list"})
    respuesta.render()
    return respuesta


def test_filtra_por_un_contrato_ppa_especifico(datos):
    con_ppa = _proyecto(nombre_comercial="Con PPA")
    _proyecto(nombre_comercial="Sin PPA")
    contrato = _contrato(nombre_interno="Contrato A")
    _vincular(con_ppa, contrato)

    respuesta = _listar(datos, f"?ppa_id={contrato.id}")

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["total"] == 1
    assert [p["id"] for p in respuesta.data["items"]] == [con_ppa.id]


def test_filtra_por_varios_contratos_ppa(datos):
    p1 = _proyecto(nombre_comercial="Uno")
    p2 = _proyecto(nombre_comercial="Dos")
    p3 = _proyecto(nombre_comercial="Tres")
    c1 = _contrato(nombre_interno="Contrato 1")
    c2 = _contrato(nombre_interno="Contrato 2")
    _vincular(p1, c1)
    _vincular(p2, c2)

    respuesta = _listar(datos, f"?ppa_id={c1.id}&ppa_id={c2.id}")

    assert respuesta.status_code == 200, respuesta.data
    assert {p["id"] for p in respuesta.data["items"]} == {p1.id, p2.id}
    assert p3.id not in {p["id"] for p in respuesta.data["items"]}


def test_filtra_proyectos_sin_ningun_ppa(datos):
    con_ppa = _proyecto(nombre_comercial="Con PPA")
    sin_ppa = _proyecto(nombre_comercial="Sin PPA")
    contrato = _contrato(nombre_interno="Contrato A")
    _vincular(con_ppa, contrato)

    respuesta = _listar(datos, "?sin_ppa=true")

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["total"] == 1
    assert [p["id"] for p in respuesta.data["items"]] == [sin_ppa.id]


def test_combina_contrato_especifico_con_sin_ppa_como_or(datos):
    seleccionado = _proyecto(nombre_comercial="Seleccionado")
    otro_ppa = _proyecto(nombre_comercial="Otro PPA")
    sin_ppa = _proyecto(nombre_comercial="Sin PPA")
    c1 = _contrato(nombre_interno="Contrato 1")
    c2 = _contrato(nombre_interno="Contrato 2")
    _vincular(seleccionado, c1)
    _vincular(otro_ppa, c2)

    respuesta = _listar(datos, f"?ppa_id={c1.id}&sin_ppa=true")

    assert respuesta.status_code == 200, respuesta.data
    assert {p["id"] for p in respuesta.data["items"]} == {seleccionado.id, sin_ppa.id}


def test_un_proyecto_con_dos_contratos_seleccionados_no_se_duplica(datos):
    p = _proyecto(nombre_comercial="Doble PPA")
    c1 = _contrato(nombre_interno="Contrato 1")
    c2 = _contrato(nombre_interno="Contrato 2")
    _vincular(p, c1)
    _vincular(p, c2)

    respuesta = _listar(datos, f"?ppa_id={c1.id}&ppa_id={c2.id}")

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["total"] == 1
    assert [p_["id"] for p_ in respuesta.data["items"]] == [p.id]


def test_sin_filtro_de_ppa_trae_todos_como_antes(datos):
    _proyecto(nombre_comercial="Con PPA")
    _proyecto(nombre_comercial="Sin PPA")

    respuesta = _listar(datos)

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["total"] == 2


# ── Contratos borrados (borrado logico) ──────────────────────────────────────
# Igual que en FastAPI: borrar un contrato PPA solo pone deleted_at, la fila de
# ppa_contrato_proyectos no se limpia -- un contrato borrado debe tratarse
# como si no existiera.

def test_ppa_id_ignora_contrato_borrado(datos):
    from datetime import datetime, timezone

    proyecto = _proyecto(nombre_comercial="Vinculado a borrado")
    contrato = _contrato(nombre_interno="Borrado", deleted_at=datetime.now(timezone.utc))
    _vincular(proyecto, contrato)

    respuesta = _listar(datos, f"?ppa_id={contrato.id}")

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["total"] == 0


def test_sin_ppa_incluye_proyecto_cuyo_unico_contrato_esta_borrado(datos):
    from datetime import datetime, timezone

    proyecto = _proyecto(nombre_comercial="Solo tenia el borrado")
    contrato = _contrato(nombre_interno="Borrado", deleted_at=datetime.now(timezone.utc))
    _vincular(proyecto, contrato)

    respuesta = _listar(datos, "?sin_ppa=true")

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["total"] == 1
    assert [p["id"] for p in respuesta.data["items"]] == [proyecto.id]


def test_ppa_id_no_numerico_da_422_no_500(datos):
    """`portafolio_id` usa `par.entero` y ya daba 422 ante un valor invalido;
    `ppa_id` debe comportarse igual, no reventar con un ValueError sin capturar."""
    respuesta = _listar(datos, "?ppa_id=abc")

    assert respuesta.status_code == 422, respuesta.data
