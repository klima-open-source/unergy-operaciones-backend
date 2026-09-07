"""Todo `ModelSerializer` de `api/v1/` tiene que poder construirse.

Bug real (2026-09-05): los cuatro endpoints de `mantenimiento-impacto`
respondian **500 en cada peticion** desde el despliegue de la migracion a
Django. `ImpactoSerializer.Meta.fields` pedia `duration_hours`, que en
SQLAlchemy era un `@hybrid_property` del modelo:

    ImproperlyConfigured: Field name `duration_hours` is not valid for model
    `MantenimientoImpacto` in ...ImpactoSerializer.

La causa es de la migracion misma, y por eso puede repetirse: los modelos de
`apps/*/models.py` los **genero** `scripts/generar_modelos_django.py` desde los
metadatos de SQLAlchemy, y ese generador lee COLUMNAS. Una propiedad de Python
no aparece en los metadatos, asi que se quedo atras mientras el serializer
seguia pidiendola.

Es la misma clase de bug que el 500 de fronteras (`select_related("operador")`
cuando el campo es `operador_red`, ver test_querysets_compilan.py): un nombre
que no existe, que nada valida hasta que alguien pide la vista. Los 2675 tests
pasaban con los dos endpoints caidos.

Construir el serializer es suficiente: DRF resuelve `Meta.fields`/`exclude`
contra el modelo en ese momento y levanta `ImproperlyConfigured` sobre cualquier
nombre que no sea campo, propiedad ni metodo `get_<campo>`. No hace falta base
de datos.
"""
import importlib
import inspect
import pkgutil

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():  # noqa: PT004
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _model_serializers():
    """`(ruta, clase)` de cada ModelSerializer declarado bajo `api/v1/`."""
    from rest_framework import serializers

    import api.v1 as raiz

    for info in pkgutil.iter_modules(raiz.__path__):
        try:
            modulo = importlib.import_module(f"api.v1.{info.name}.serializers")
        except ModuleNotFoundError:
            continue  # no todos los recursos tienen serializers.py
        for nombre, cls in vars(modulo).items():
            if not inspect.isclass(cls):
                continue
            if not issubclass(cls, serializers.ModelSerializer):
                continue
            if cls.__module__ != modulo.__name__:
                continue  # importado de otro lado
            if getattr(getattr(cls, "Meta", None), "model", None) is None:
                continue  # base abstracta
            yield f"{modulo.__name__}.{nombre}", cls


def test_hay_serializers_que_revisar():
    """Sanity: si el recorrido deja de encontrarlos, el test pasaria vacio."""
    encontrados = list(_model_serializers())
    assert len(encontrados) >= 50, f"solo se encontraron {len(encontrados)}"


def test_todo_model_serializer_se_puede_construir():
    from django.core.exceptions import ImproperlyConfigured

    fallos = []
    for ruta, cls in _model_serializers():
        try:
            cls().fields  # noqa: B018 -- construir los campos es lo que valida
        except (ImproperlyConfigured, ValueError, AssertionError) as exc:
            # Las tres: DRF usa `ValueError` (default sobre un m2m) y
            # `AssertionError` (campo declarado fuera de `fields`) para decir lo
            # mismo que `ImproperlyConfigured`. Con solo la primera, este test
            # pasaba con el POST de fallas caido.
            fallos.append(f"{ruta} -> {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001 -- del entorno, no de esta clase de bug
            continue

    assert not fallos, (
        "Hay serializers que no se pueden construir. Cada endpoint que los use "
        "responde 500 en todas sus peticiones:\n  "
        + ("\n  ").join(fallos)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Los dos guards de abajo son la parte que faltaba (2026-09-07).
#
# `test_todo_model_serializer_se_puede_construir` pasaba con `POST /api/v1/fallas`
# respondiendo 500 en TODA peticion, que es exactamente la clase de bug que dice
# vigilar. Dos agujeros:
#
#   1. El `except Exception: continue` se tragaba el fallo. DRF usa las tres —
#      `ImproperlyConfigured`, `ValueError` y `AssertionError` — para decir
#      "serializer mal declarado": `FallaCrearSerializer` levantaba `ValueError`
#      (`default=None` sobre un m2m) y `FronteraSerializer` `AssertionError`.
#   2. Solo recorria los serializers declarados a nivel de modulo. Los de
#      escritura se eligen por accion en `get_serializer_class()`, asi que hay que
#      preguntarle al viewset cual usa en `create`/`update`/`partial_update`.
#
# Y un tercer guard para el bug que NO es de construccion: un serializer que se
# construye perfecto y descarta en silencio lo que le mandan (ver el comentario de
# `_COLUMNAS_ENTRADA` en api/v1/fallas/serializers.py).
# ─────────────────────────────────────────────────────────────────────────────

ACCIONES_DE_ESCRITURA = ("create", "update", "partial_update")

# Las tres que DRF levanta al construir un serializer mal declarado.
ERRORES_DE_DECLARACION: tuple[type[Exception], ...] = ()


@pytest.fixture(scope="module", autouse=True)
def _errores(_django_listo):
    global ERRORES_DE_DECLARACION
    from django.core.exceptions import ImproperlyConfigured

    ERRORES_DE_DECLARACION = (ImproperlyConfigured, ValueError, AssertionError)


def _viewsets():
    """`(ruta, clase)` de cada viewset declarado en `api/v1/*/views.py`."""
    from rest_framework.viewsets import ViewSetMixin

    import api.v1 as raiz

    for info in pkgutil.iter_modules(raiz.__path__):
        try:
            modulo = importlib.import_module(f"api.v1.{info.name}.views")
        except ModuleNotFoundError:
            continue
        for nombre, cls in vars(modulo).items():
            if not inspect.isclass(cls) or not issubclass(cls, ViewSetMixin):
                continue
            if cls.__module__ != modulo.__name__:
                continue  # importado de otro lado
            yield f"{modulo.__name__}.{nombre}", cls


def _serializers_de_escritura():
    """`(ruta, accion, clase)` del serializer que cada viewset usa para escribir.

    Solo las acciones que el viewset implementa de verdad: preguntar por `create`
    a un viewset de solo lectura no dice nada.
    """
    for ruta, cls in _viewsets():
        for accion in ACCIONES_DE_ESCRITURA:
            if not hasattr(cls, accion):
                continue
            vista = cls()
            vista.action = accion
            vista.request = None
            vista.format_kwarg = None
            vista.kwargs = {}
            try:
                serializador = vista.get_serializer_class()
            except Exception:  # noqa: BLE001 -- sin serializer_class, o mira el request
                continue
            if serializador is None:
                continue
            yield f"{ruta}.{accion}", accion, serializador


def test_hay_serializers_de_escritura_que_revisar():
    """Sanity: si el recorrido deja de encontrarlos, los dos tests de abajo
    pasarian vacios."""
    encontrados = list(_serializers_de_escritura())
    assert len(encontrados) >= 20, f"solo se encontraron {len(encontrados)}"


def test_todo_serializer_de_escritura_se_puede_construir():
    """El serializer que atiende un POST/PATCH tiene que construirse.

    Si no, el endpoint responde 500 antes de validar nada — no hay camino que
    funcione. Esta es la aserción que falla con el `default=None` de
    `FallaCrearSerializer.intervalos` puesto.
    """
    fallos = []
    for ruta, _accion, cls in _serializers_de_escritura():
        try:
            cls().fields  # noqa: B018 -- construir los campos es lo que valida
        except ERRORES_DE_DECLARACION as exc:
            fallos.append(f"{ruta} -> {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001 -- del entorno, no de esta clase de bug
            continue

    assert not fallos, (
        "Hay serializers de escritura que no se pueden construir. El endpoint "
        "que los use responde 500 en todas sus peticiones:\n  "
        + "\n  ".join(fallos)
    )


def test_ningun_serializer_de_escritura_descarta_campos_en_silencio():
    """Ningún campo de un serializer de escritura puede ser `ReadOnlyField`.

    Es lo que produce `ModelSerializer.build_property_field` cuando el nombre en
    `Meta.fields` no es un campo del modelo sino una propiedad o un *attname* de
    FK (`estado_id`). El serializer se construye perfecto, `is_valid()` devuelve
    `True` sin errores y el valor **desaparece** de `validated_data`: el cliente
    recibe 200 y nada se escribió. Paso con los cinco `*_id` de
    `FallaActualizarSerializer` (2026-09-07).

    En salida un `ReadOnlyField` da el valor correcto, y por eso el guard es solo
    para escritura.
    """
    from rest_framework import serializers

    fallos = []
    for ruta, _accion, cls in _serializers_de_escritura():
        try:
            campos = cls().fields
        except Exception:  # noqa: BLE001 -- lo reporta el test de arriba
            continue
        mudos = [n for n, f in campos.items() if isinstance(f, serializers.ReadOnlyField)]
        if mudos:
            fallos.append(f"{ruta} -> {', '.join(mudos)}")

    assert not fallos, (
        "Estos serializers de escritura declaran campos como ReadOnlyField: "
        "aceptan el valor con 200 y lo descartan sin decir nada. Decláralos a "
        "mano (p.ej. los `*_id` de FK como IntegerField):\n  "
        + "\n  ".join(fallos)
    )
