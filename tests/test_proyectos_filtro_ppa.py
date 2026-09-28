"""Filtro por contrato PPA en GET /proyectos (ver docs/API_PROYECTOS.md).

"PPA" se puede filtrar de dos formas distintas y no hay que confundirlas:
`servicio=ppa` es la bandera de servicio contratado (columna booleana
`srv_ppa`), mientras que `ppa_id` (repetible) y `sin_ppa` miran el vínculo a un
contrato PPA real (tabla `ppa_contratos`, via `ppa_contrato_proyectos`).

**El port a Django perdió los dos.** `api/v1/proyectos/views.py::list` solo
implementaba `q`, `estado`, `tipo_proyecto`, `portafolio_id` y `servicio`, así
que el MultiSelect de PPA de la página de Proyectos mandaba `ppa_id`/`sin_ppa`
y el backend los ignoraba en silencio: 200 con el listado completo, como si el
filtro no se hubiera tocado. Estas pruebas existían desde que el filtro se
escribió, pero llamaban a `app/api/v1/proyectos.py` — el árbol FastAPI apagado
—, así que seguían verdes mientras lo que corre en producción no filtraba nada.
Ahora apuntan al ViewSet de Django, que es lo que se sirve.

Mismo patrón de bug que documentan tests/test_fallas_django.py y
tests/test_contratos_servicio_proyecto_anidado_django.py: algo que el frontend
necesita, silenciosamente ausente en el port.
"""
from datetime import datetime, timezone

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


def _listar(datos_usuario, querystring=""):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.proyectos.views import ProyectoViewSet

    peticion = APIRequestFactory().get(f"/api/v1/proyectos{querystring}")
    force_authenticate(peticion, user=UsuarioAutenticado(datos_usuario["usuario"]))
    respuesta = ProyectoViewSet.as_view({"get": "list"})(peticion)
    respuesta.render()
    return respuesta


def _ids(respuesta):
    return {p["id"] for p in respuesta.data["items"]}


def _proyecto(nombre_comercial="Proyecto", **kw):
    from apps.proyectos.models import Proyecto

    return Proyecto.objects.create(nombre_comercial=nombre_comercial, **kw)


def _contrato(**kw):
    from apps.ppa.models import PpaContrato

    return PpaContrato.objects.create(**kw)


def _vincular(proyecto, contrato):
    from apps.ppa.models import PpaContratoProyecto

    PpaContratoProyecto.objects.create(proyecto=proyecto, contrato=contrato)


def test_filtra_por_un_contrato_ppa_especifico(datos):
    con_ppa = _proyecto("Con PPA")
    _proyecto("Sin PPA")
    contrato = _contrato(nombre_interno="Contrato A")
    _vincular(con_ppa, contrato)

    respuesta = _listar(datos, f"?ppa_id={contrato.id}")

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["total"] == 1
    assert _ids(respuesta) == {con_ppa.id}


def test_filtra_por_varios_contratos_ppa(datos):
    """El parámetro repetido: `?ppa_id=1&ppa_id=2`.

    FastAPI lo parseaba por la firma (`list[int]`); en DRF hay que leerlo con
    `getlist` — `query_params.get` devolvería SOLO el último y el filtro
    quedaría a medias sin que se note.
    """
    p1 = _proyecto("Uno")
    p2 = _proyecto("Dos")
    p3 = _proyecto("Tres")
    c1 = _contrato(nombre_interno="Contrato 1")
    c2 = _contrato(nombre_interno="Contrato 2")
    _vincular(p1, c1)
    _vincular(p2, c2)

    respuesta = _listar(datos, f"?ppa_id={c1.id}&ppa_id={c2.id}")

    assert _ids(respuesta) == {p1.id, p2.id}
    assert p3.id not in _ids(respuesta)


def test_filtra_proyectos_sin_ningun_ppa(datos):
    con_ppa = _proyecto("Con PPA")
    sin_ppa = _proyecto("Sin PPA")
    _vincular(con_ppa, _contrato(nombre_interno="Contrato A"))

    respuesta = _listar(datos, "?sin_ppa=true")

    assert respuesta.data["total"] == 1
    assert _ids(respuesta) == {sin_ppa.id}


def test_combina_contrato_especifico_con_sin_ppa_como_or(datos):
    seleccionado = _proyecto("Seleccionado")
    con_otro = _proyecto("Otro PPA")
    sin_ppa = _proyecto("Sin PPA")
    c1 = _contrato(nombre_interno="Contrato 1")
    c2 = _contrato(nombre_interno="Contrato 2")
    _vincular(seleccionado, c1)
    _vincular(con_otro, c2)

    respuesta = _listar(datos, f"?ppa_id={c1.id}&sin_ppa=true")

    assert _ids(respuesta) == {seleccionado.id, sin_ppa.id}
    assert con_otro.id not in _ids(respuesta)


def test_un_proyecto_con_dos_contratos_seleccionados_no_se_duplica(datos):
    """Con un `join` + `distinct` esto sale una vez, pero el `total` de la
    paginación cuenta dos: la fila se duplica antes del `distinct`. De ahí el
    `Exists` del filtro."""
    proyecto = _proyecto("Doble PPA")
    c1 = _contrato(nombre_interno="Contrato 1")
    c2 = _contrato(nombre_interno="Contrato 2")
    _vincular(proyecto, c1)
    _vincular(proyecto, c2)

    respuesta = _listar(datos, f"?ppa_id={c1.id}&ppa_id={c2.id}")

    assert respuesta.data["total"] == 1
    assert [p["id"] for p in respuesta.data["items"]] == [proyecto.id]


def test_sin_filtro_de_ppa_trae_todos_como_antes(datos):
    _proyecto("Con PPA")
    _proyecto("Sin PPA")

    respuesta = _listar(datos)

    assert respuesta.data["total"] == 2


def test_se_combina_con_los_otros_filtros_del_listado(datos):
    """El MultiSelect de PPA no es el único filtro de la vista: convive con
    Estado, Tipo y Portafolio, que se aplican con AND."""
    operando = _proyecto("Operando", estado="en_operacion")
    desarrollo = _proyecto("En desarrollo", estado="en_desarrollo")
    contrato = _contrato(nombre_interno="Contrato A")
    _vincular(operando, contrato)
    _vincular(desarrollo, contrato)

    respuesta = _listar(datos, f"?ppa_id={contrato.id}&estado=en_operacion")

    assert _ids(respuesta) == {operando.id}


# ── Contratos borrados (borrado lógico) ──────────────────────────────────────
# Borrar un contrato PPA solo pone deleted_at; la fila de la tabla puente
# ppa_contrato_proyectos no se limpia. El resto de la API ya trata un PPA
# borrado como inexistente (get_ppa_contratos en api/v1/proyectos/serializers.py),
# y este filtro tiene que hacer lo mismo.

def test_ppa_id_ignora_contrato_borrado(datos):
    proyecto = _proyecto("Vinculado a borrado")
    contrato = _contrato(nombre_interno="Borrado", deleted_at=datetime.now(timezone.utc))
    _vincular(proyecto, contrato)

    respuesta = _listar(datos, f"?ppa_id={contrato.id}")

    assert respuesta.data["total"] == 0


def test_sin_ppa_incluye_proyecto_cuyo_unico_contrato_esta_borrado(datos):
    proyecto = _proyecto("Solo tenía el borrado")
    contrato = _contrato(nombre_interno="Borrado", deleted_at=datetime.now(timezone.utc))
    _vincular(proyecto, contrato)

    respuesta = _listar(datos, "?sin_ppa=true")

    assert respuesta.data["total"] == 1
    assert _ids(respuesta) == {proyecto.id}


# ── Parámetros inválidos ─────────────────────────────────────────────────────

def test_ppa_id_no_numerico_da_422_y_no_500(datos):
    """`[int(v) for v in getlist(...)]` levantaría un ValueError, y eso sale
    como 500: parece una caída del servidor y no un parámetro mal escrito."""
    _proyecto("Cualquiera")

    respuesta = _listar(datos, "?ppa_id=doce")

    assert respuesta.status_code == 422, respuesta.data


def test_ppa_id_vacio_no_filtra(datos):
    """`?ppa_id=` es lo que manda un select en "Todos" mal serializado: cuenta
    como ausente, no como "el contrato de id vacío"."""
    _proyecto("Uno")
    _proyecto("Dos")

    respuesta = _listar(datos, "?ppa_id=")

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["total"] == 2
