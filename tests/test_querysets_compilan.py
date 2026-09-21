"""Toda función de `api/v1/*/queryset.py` tiene que poder compilar su SQL.

Bug real (2026-09-05): el listado de fronteras devolvía **500 en cada llamada**
desde el despliegue de la migración a Django. El campo del modelo se llama
`operador_red` y el port lo referenciaba como `operador`:

    Invalid field name(s) given in select_related: 'operador'.
    Choices are: proyecto, operador_red

En producción se vio como "0 fronteras registradas", porque `FronterasView.vue`
se traga la excepción y deja la lista vacía. El mismo error estaba en
`api/v1/polizas/queryset.py`, sobre `Proyecto` y sobre `Frontera`.

Los 2673 tests pasaban con eso roto: ninguno ejecuta estas consultas, y
`select_related` no se valida hasta que la consulta se compila.

Este test cierra ese hueco **sin necesitar base de datos**: `str(qs.query)`
compila el SQL y ahí Django resuelve cada relación contra el modelo. Un nombre
de campo inventado levanta `FieldError` en ese momento, no al ejecutar.

Solo se reporta `FieldError`, a propósito. Algunas de estas funciones hacen
trabajo real al invocarlas -- una habla con Google Drive, otra consulta la base
-- y sus fallos dependen del entorno: sin credenciales o contra un esquema
viejo revientan por motivos que no son este bug. `FieldError` no depende del
entorno: significa que el nombre del campo no existe en el modelo, y punto.
"""
import importlib
import inspect
import pkgutil

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _modulos_queryset():
    """Los `api/v1/<recurso>/queryset.py` que existan."""
    import api.v1 as raiz

    for info in pkgutil.iter_modules(raiz.__path__):
        try:
            yield importlib.import_module(f"api.v1.{info.name}.queryset")
        except ModuleNotFoundError:
            continue  # no todos los recursos tienen queryset.py


def _funciones_sin_argumentos(modulo):
    """Las que se pueden invocar sin datos: son las que arman el listado base."""
    # `list(...)`: el llamador invoca cada `fn()` mientras este generador está
    # suspendido; si esa llamada dispara un import diferido que agrega un nombre
    # al módulo, `vars(modulo)` cambia de tamaño y la iteración cruda revienta
    # ("dictionary changed size during iteration"). Copiar el dict lo evita.
    for nombre, fn in list(vars(modulo).items()):
        if nombre.startswith("_") or not inspect.isfunction(fn):
            continue
        if fn.__module__ != modulo.__name__:
            continue  # importada de otro lado
        firma = inspect.signature(fn)
        obligatorios = [
            p for p in firma.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        ]
        if not obligatorios:
            yield nombre, fn


def test_hay_querysets_que_revisar():
    """Sanity: si el recorrido deja de encontrar módulos, el test pasaría vacío."""
    modulos = list(_modulos_queryset())
    assert len(modulos) >= 5, f"solo se encontraron {len(modulos)} módulos queryset"


def test_todo_queryset_compila_su_sql():
    """Compilar es suficiente: ahí Django resuelve select_related/prefetch_related
    contra el modelo, y un campo inexistente levanta FieldError."""
    from django.core.exceptions import FieldError

    fallos = []
    revisados = 0

    for modulo in _modulos_queryset():
        for nombre, fn in _funciones_sin_argumentos(modulo):
            try:
                resultado = fn()
            except FieldError as exc:
                fallos.append(f"{modulo.__name__}.{nombre}() -> {exc}")
                continue
            except Exception:  # noqa: BLE001 -- del entorno, no de este bug
                continue

            consulta = getattr(resultado, "query", None)
            if consulta is None:
                continue  # no devolvió un QuerySet: no aplica
            revisados += 1
            try:
                str(consulta)
            except FieldError as exc:
                fallos.append(f"{modulo.__name__}.{nombre}() al compilar -> {exc}")
            except Exception:  # noqa: BLE001
                pass

    assert revisados > 0, "no se compiló ningún queryset -- revisar el recorrido"
    assert not fallos, (
        "Hay querysets que no compilan. Cada uno de estos endpoints responde 500 "
        "en todas sus peticiones, y el frontend lo muestra como una lista vacía:\n  "
        + "\n  ".join(fallos)
    )
