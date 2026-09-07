"""Los seis endpoints de fallas del árbol Django, ejercidos de verdad.

Los 65 tests de `tests/test_fallas*.py` prueban FastAPI (`app/api/v1/fallas.py`).
Django/DRF ya lo reemplazó en producción y **ningún test importaba el árbol
nuevo**, así que estos tres 500 vivieron en producción sin que nada los viera:

  * `POST /api/v1/fallas` — `ValueError` al construir `FallaCrearSerializer`
    (`default=None` sobre `intervalos`, que es un m2m inverso). 500 en TODA
    petición, antes de validar nada.
  * `PATCH /api/v1/fallas/{id}` — los cinco `*_id` de FK caían en `ReadOnlyField`,
    así que `validated_data` salía sin ellos: 200 con el cuerpo viejo, sin cambio
    de estado, sin `fecha_resolucion` ni `sla_cumplido`, y el bloqueo de
    "pendiente de reclasificar" era código muerto en esa ruta.
  * `GET /api/v1/fallas/por-proyecto` — `falla.dias_abierta` ya no existe en el
    modelo (la lógica vive en `dominio`). Solo se veía con `items` no vacío.

Más dos derivas de forma: los `Decimal` salían como string ("2205.225") y la
paginación usaba 50/500 en vez de los 20/5000 de FastAPI, con el alias
`page_size` roto.

No hay `pytest-django`: el fixture arma una base sqlite en memoria con
`create_test_db` y `MIGRATION_MODULES` a `None` para todas las apps, o sea las
tablas salen del estado actual de los modelos. Se salta así la cadena de
migraciones de Django, que hoy no corre desde cero (`OportunidadOfertaProyecto`
tiene un pk compuesto que su 0001 no refleja) — un problema de otra app, ajeno a
esto.
"""
import json
from datetime import date

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

    # Si otro modulo del suite ya llamo `django.setup()`, el `ConnectionHandler`
    # tiene `settings` cacheado (es un `cached_property`) y el parche de arriba
    # llega tarde: `create_test_db` se iria contra el Postgres real a crear
    # `test_operaciones`. Hay que invalidar el cache y soltar la conexion viva.
    connections.close_all()
    connections.__dict__.pop("settings", None)  # el cached_property
    connections.__init__()  # rehace `_settings` y el Local de conexiones

    settings.MIGRATION_MODULES = {a.label: None for a in django_apps.get_app_configs()}
    setup_test_environment()
    connections["default"].creation.create_test_db(verbosity=0)
    assert connections["default"].vendor == "sqlite", "no se aisló de la base real"
    yield

    # Devolver el handler a la configuración real: si no, el módulo que corra
    # después de este se encuentra la base en sqlite sin haberlo pedido.
    from django.test.utils import teardown_test_environment

    connections.close_all()
    teardown_test_environment()
    settings.DATABASES = originales
    connections.__dict__.pop("settings", None)
    connections.__init__()


@pytest.fixture
def datos():
    """Un proyecto, los catálogos mínimos y un usuario admin.

    Sin `pytest-django` no hay aislamiento automático: la transacción se abre acá
    y se revierte al terminar, para que la base en memoria vuelva vacía y los
    tests no se pisen (`usuarios.email` es único).
    """
    from django.db import transaction

    from apps.monitoreo import models as mo
    from apps.plataforma.models import Usuario
    from apps.proyectos.models import Proyecto

    atomica = transaction.atomic()
    atomica.__enter__()
    proyecto = Proyecto.objects.create(nombre_comercial="Planta Prueba")
    abierto = mo.FallaCatEstado.objects.create(
        codigo="abierta", etiqueta="Abierta", orden=1, es_estado_final=False
    )
    cerrado = mo.FallaCatEstado.objects.create(
        codigo="cerrada", etiqueta="Cerrada", orden=9, es_estado_final=True
    )
    alta = mo.FallaCatPrioridad.objects.create(codigo="alta", etiqueta="Alta", nivel=1)
    usuario = Usuario.objects.create(
        nombre="QA", email="qa@unergy.io", rol="admin", activo=True
    )
    yield {
        "proyecto": proyecto, "abierto": abierto, "cerrado": cerrado,
        "alta": alta, "usuario": usuario,
    }
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _pedir(metodo, url, datos_usuario, cuerpo=None, **kwargs):
    """Llama al `FallaViewSet` como lo haría el router, sin levantar servidor."""
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.fallas.views import FallaViewSet

    factory = APIRequestFactory()
    peticion = getattr(factory, metodo)(url, cuerpo, format="json") if cuerpo is not None \
        else getattr(factory, metodo)(url)
    # El `Usuario` crudo no sirve: `RolePermission` lee `.roles` (lista) y el
    # modelo tiene `.rol` singular -> 403.
    force_authenticate(peticion, user=UsuarioAutenticado(datos_usuario["usuario"]))
    vista = FallaViewSet.as_view(kwargs.pop("acciones"))
    return vista(peticion, **kwargs)


def _falla(datos, **extra):
    from apps.monitoreo import models as mo

    campos = {
        "proyecto_id": datos["proyecto"].id,
        "estado_id": datos["abierto"].id,
        "prioridad_id": datos["alta"].id,
        "descripcion": "inversor caido",
        "fecha_identificacion": date(2026, 9, 1),
        "registrado_por_id": datos["usuario"].id,
    }
    campos.update(extra)
    falla = mo.Falla.objects.create(codigo_interno="FAL-2026-00001", **campos)
    return falla


# ── P0-1 · el POST respondía 500 en toda petición ─────────────────────────────

def test_post_crea_la_falla_y_no_revienta(datos):
    respuesta = _pedir(
        "post", "/api/v1/fallas",
        datos,
        {
            "proyecto_id": datos["proyecto"].id,
            "estado_id": datos["abierto"].id,
            "prioridad_id": datos["alta"].id,
            "descripcion": "inversor caido",
            "fecha_identificacion": "2026-09-01",
        },
        acciones={"post": "create"},
    )
    assert respuesta.status_code == 201, respuesta.data
    assert respuesta.data["codigo_interno"].startswith("FAL-")


# ── P0-2 · el PATCH devolvía 200 y descartaba el estado ───────────────────────

def test_patch_de_estado_final_sella_resolucion_y_sla(datos):
    falla = _falla(datos)
    respuesta = _pedir(
        "patch", f"/api/v1/fallas/{falla.id}",
        datos, {"estado_id": datos["cerrado"].id},
        acciones={"patch": "partial_update"}, pk=falla.id,
    )
    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["estado"]["codigo"] == "cerrada"
    # Las dos que `dominio.sincronizar_resolucion` escribe al cerrar. Con los
    # `*_id` en ReadOnlyField ni se llamaba: quedaban en None.
    assert respuesta.data["fecha_resolucion"] is not None
    assert respuesta.data["sla_cumplido"] is not None


def test_patch_no_cierra_una_falla_pendiente_de_reclasificar(datos):
    falla = _falla(datos, pendiente_reclasificar=True)
    respuesta = _pedir(
        "patch", f"/api/v1/fallas/{falla.id}",
        datos, {"estado_id": datos["cerrado"].id},
        acciones={"patch": "partial_update"}, pk=falla.id,
    )
    assert respuesta.status_code == 409, respuesta.data
    falla.refresh_from_db()
    assert falla.estado_id == datos["abierto"].id


# ── P0-3 · `por-proyecto` reventaba en cuanto devolvía filas ──────────────────
#
# El test de este caso se retiro junto con el endpoint (2026-09-07): no lo
# consumia ningun flujo interno ni el frontend. El arreglo que lo motivo NO se
# perdio: `dominio.dias_abierta` / `dominio.tiempo_afectacion_horas` --las dos
# propiedades que el port a Django no habia traido-- las sigue usando el
# serializer del detalle (api/v1/fallas/serializers.py:146).


# ── P1-4 y P1-5 · forma de la respuesta ───────────────────────────────────────

def test_los_decimales_salen_como_numero_no_como_string(datos):
    falla = _falla(datos, kwh_perdidos_estimado="2205.225")
    respuesta = _pedir(
        "get", f"/api/v1/fallas/{falla.id}", datos,
        acciones={"get": "retrieve"}, pk=falla.id,
    )
    assert respuesta.status_code == 200, respuesta.data
    # Sobre el JSON renderizado y no sobre `.data`, que ahi sigue siendo Decimal:
    # lo que importa es lo que sale por el cable. `COERCE_DECIMAL_TO_STRING` viene
    # en True por default y mandaba "2205.225" — cualquier aritmetica del frontend
    # sobre eso da NaN.
    cuerpo = json.loads(respuesta.render().content)
    assert isinstance(cuerpo["kwh_perdidos_estimado"], float), cuerpo["kwh_perdidos_estimado"]


@pytest.mark.parametrize(
    ("consulta", "esperado"),
    [
        ("", 20),              # el default de FastAPI, no el 50 de BasePagination
        ("?page_size=4", 4),   # el alias historico, que era un no-op
        ("?size=1000", 1000),  # antes recortaba callado a max_page_size=500
    ],
)
def test_paginacion_igual_a_fastapi(datos, consulta, esperado):
    _falla(datos)
    respuesta = _pedir(
        "get", f"/api/v1/fallas{consulta}", datos, acciones={"get": "list"},
    )
    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["size"] == esperado
