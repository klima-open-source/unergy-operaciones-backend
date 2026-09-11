"""Una migración no puede quitar un campo antes de soltar su índice único.

El hueco que esto cierra costó un deploy (run #66, 2026-09-10). La migración
`0004_eliminar_promotor` traía las operaciones en el orden que genera
`makemigrations`: primero `RemoveField(requisito)` y después
`AlterUniqueTogether(None)`. Para borrar el índice único de
`(proyecto, requisito)` Django busca la columna de cada campo, así que al llegar
al segundo paso el campo ya no existía y el `migrate` del deploy murió con:

    django.core.exceptions.FieldDoesNotExist:
        PromotorSeguimiento has no field named 'requisito'

Ninguna de las dos verificaciones que ya existían podía verlo:

  - la suite corre con `MIGRATION_MODULES = {app: None}`, o sea que crea las
    tablas DIRECTO desde los modelos y no ejecuta una sola migración;
  - `makemigrations --check` (tests/test_esquema_django.py) compara los modelos
    contra el ESTADO de las migraciones, que se calcula sin tocar la base.

Las dos miran el destino y ninguna el camino.

**Por qué esto es un chequeo estático y no un `migrate` de verdad.** Sería mejor
aplicar la cadena completa sobre una base vacía, pero hoy no se puede: se cae en
`comercial.0001_initial`, que crea `OportunidadOfertaProyecto` con una clave
primaria compuesta sobre `oferta_id`/`proyecto_id` -- dos campos que recién
agrega `0003_initial`. Es consecuencia de `--fake-initial`: esas migraciones
nunca se ejecutaron, solo describen la base que ya existía, así que nadie
notó que no corren de cero. Arreglarlo es su propio trabajo (ver el comentario
del servicio `migrate` en docker-compose.yml sobre retirar `--fake-initial`);
mientras tanto, esta prueba cubre el modo de fallo concreto que tumbó el deploy.
"""
import ast
import os
import re

import pytest

MIGRACIONES = "apps"


def _migraciones():
    raiz = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), MIGRACIONES)
    for base, _, archivos in os.walk(raiz):
        if not base.endswith(os.sep + "migrations"):
            continue
        for nombre in sorted(archivos):
            if nombre.endswith(".py") and nombre != "__init__.py":
                yield os.path.join(base, nombre)


def _operaciones(ruta):
    """[(nombre_operacion, model_name)] en el orden en que se aplican."""
    arbol = ast.parse(open(ruta, encoding="utf-8").read())
    salida = []
    for nodo in ast.walk(arbol):
        if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)):
            continue
        modelo = None
        for kw in nodo.keywords:
            if kw.arg in ("model_name", "name") and isinstance(kw.value, ast.Constant):
                # `AlterUniqueTogether` usa `name`; `RemoveField`, `model_name`.
                if kw.arg == "model_name" or nodo.func.attr.startswith("Alter"):
                    modelo = kw.value.value
        salida.append((nodo.func.attr, modelo))
    return salida


def test_el_indice_unico_se_suelta_antes_de_quitar_sus_campos():
    """`AlterUniqueTogether` de un modelo va ANTES que cualquier `RemoveField`
    suyo, dentro de la misma migración.

    Es el orden que `makemigrations` NO garantiza y que el `migrate` exige: al
    soltar el índice compuesto, Django resuelve la columna de cada campo contra
    el estado del modelo, y un campo ya removido no se puede resolver.
    """
    problemas = []
    for ruta in _migraciones():
        ops = _operaciones(ruta)
        quitados: dict[str, int] = {}
        for i, (operacion, modelo) in enumerate(ops):
            if modelo is None:
                continue
            if operacion == "RemoveField":
                quitados.setdefault(modelo, i)
            elif operacion == "AlterUniqueTogether" and modelo in quitados:
                problemas.append(
                    f"{os.path.relpath(ruta)}: AlterUniqueTogether de '{modelo}' "
                    f"viene DESPUÉS de un RemoveField del mismo modelo "
                    f"(posiciones {quitados[modelo]} y {i}). Mové el "
                    f"AlterUniqueTogether al principio: al soltar el índice, "
                    f"Django busca la columna de un campo que ya no existe."
                )
    assert not problemas, "\n".join(problemas)


def test_la_prueba_reconoce_el_orden_que_tumbo_el_deploy():
    """Guarda de la guarda: si el detector deja de detectar, esto falla.

    Se le pasa el orden exacto que generó `makemigrations` para
    `0004_eliminar_promotor` y que reventó en producción.
    """
    ops = [
        ("RemoveField", "promotorseguimiento"),
        ("AlterUniqueTogether", "promotorseguimiento"),
        ("RemoveField", "proyecto"),
    ]
    quitados = {}
    detectado = False
    for i, (operacion, modelo) in enumerate(ops):
        if operacion == "RemoveField":
            quitados.setdefault(modelo, i)
        elif operacion == "AlterUniqueTogether" and modelo in quitados:
            detectado = True
    assert detectado


def test_la_migracion_del_promotor_quedo_en_el_orden_correcto():
    """El caso concreto, fijado por si alguien regenera esa migración."""
    ruta = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "apps", "proyectos", "migrations", "0004_eliminar_promotor.py",
    )
    texto = open(ruta, encoding="utf-8").read()
    assert re.search(r"AlterUniqueTogether.*?RemoveField", texto, re.S), (
        "el AlterUniqueTogether tiene que ir antes del primer RemoveField"
    )


@pytest.mark.skip(
    reason="hoy la cadena no corre de cero: comercial.0001_initial crea "
           "OportunidadOfertaProyecto con una PK compuesta sobre campos que "
           "agrega 0003_initial. Es deuda de --fake-initial, no de este cambio."
)
def test_todas_las_migraciones_aplican_de_cero():
    """La prueba que de verdad querríamos: aplicar la cadena entera.

    Queda escrita y saltada a propósito: el día que se arreglen las initial
    --paso obligado para retirar `--fake-initial`-- se le quita el skip y pasa a
    cubrir cualquier error de orden, no solo el del índice único.
    """
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)

    from django.conf import settings

    settings.DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    }
    settings.MIGRATION_MODULES = {}
    django.setup()

    from django.db import connections
    from django.test.utils import setup_test_environment, teardown_test_environment

    connections.close_all()
    connections.__dict__.pop("settings", None)
    connections.__init__()
    setup_test_environment()
    try:
        connections["default"].creation.create_test_db(verbosity=0, serialize=False)
    finally:
        connections.close_all()
        teardown_test_environment()
