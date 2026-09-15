"""`codigo_tsf` y `origina_code` no se pueden repetir entre proyectos vivos.

Son los dos códigos con que la sincronización de Sun Factory decide si un
proyecto que llega YA existe acá. Se usaban para eso desde siempre, pero nada
impedía crear dos filas con el mismo valor: la protección era detección, no
prevención.

El caso real: **"Astrea 1 (Calipso)", ids 274 y 275, mismo `codigo_tsf`**. El
código lo registró una persona al crear el proyecto a mano, Sun Factory mandó el
suyo, y quedaron dos. `tsf_sync` lo detecta y lo anota en `ambiguos`... que no lo
lee nadie. Detectar después no es proteger.

Dos decisiones de la restricción:

  · **Solo entre vivos.** Un merge deja al perdedor con `deleted_at`, y su
    código tiene que quedar libre. Sin esa condición, un proyecto borrado
    ocuparía su código para siempre y no se podría volver a crear.
  · **`NULL` no cuenta.** 67 de 188 proyectos no tienen `codigo_tsf`, y en
    Postgres `NULL != NULL` dentro de un índice único -- pueden convivir todos.

Verificado contra producción antes de crearla (2026-09-15): cero duplicados en
los dos campos y cero cadenas vacías. La cadena vacía SÍ chocaría, porque `''`
es un valor como cualquier otro.
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
def base_limpia():
    from django.db import transaction

    atomica = transaction.atomic()
    atomica.__enter__()
    yield
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _crear(nombre, **kw):
    from apps.proyectos.models import Proyecto

    return Proyecto.objects.create(nombre_comercial=nombre, **kw)


def _es_conflicto(excinfo):
    from django.db import IntegrityError

    return excinfo.type is IntegrityError


# ── codigo_tsf ──────────────────────────────────────────────────────────────


def test_dos_proyectos_vivos_no_comparten_codigo_tsf(base_limpia):
    """El caso Astrea: uno creado a mano y otro que llegó de Sun Factory."""
    from django.db import IntegrityError

    _crear("Astrea 1 (Calipso)", codigo_tsf="COLCEST55P2")

    with pytest.raises(IntegrityError):
        _crear("Astrea 1", codigo_tsf="COLCEST55P2")


def test_muchos_proyectos_pueden_no_tener_codigo_tsf(base_limpia):
    """67 de 188 no lo tienen: `NULL` no choca con `NULL`."""
    _crear("Sin Codigo A")
    _crear("Sin Codigo B")
    _crear("Sin Codigo C")

    from apps.proyectos.models import Proyecto

    assert Proyecto.objects.filter(codigo_tsf__isnull=True).count() == 3


def test_un_proyecto_borrado_libera_su_codigo(base_limpia):
    """Un merge deja al perdedor con soft-delete. Si siguiera ocupando el
    código, el ganador no podría quedarse con él."""
    from django.utils import timezone

    _crear("Perdedor Del Merge", codigo_tsf="COLCEST55P2",
           deleted_at=timezone.now())

    vivo = _crear("Ganador Del Merge", codigo_tsf="COLCEST55P2")

    assert vivo.pk is not None


def test_dos_borrados_tampoco_chocan_entre_si(base_limpia):
    """La condición los deja a los dos fuera del índice."""
    from django.utils import timezone

    ahora = timezone.now()
    _crear("Borrado A", codigo_tsf="COLXXX1", deleted_at=ahora)
    _crear("Borrado B", codigo_tsf="COLXXX1", deleted_at=ahora)

    from apps.proyectos.models import Proyecto

    assert Proyecto.objects.filter(codigo_tsf="COLXXX1").count() == 2


# ── origina_code ────────────────────────────────────────────────────────────


def test_dos_proyectos_vivos_no_comparten_origina_code(base_limpia):
    from django.db import IntegrityError

    _crear("Monterrubio", origina_code="COLMAGT6P1_SABANAS_NORTE")

    with pytest.raises(IntegrityError):
        _crear("Monterrubio 2", origina_code="COLMAGT6P1_SABANAS_NORTE")


def test_origina_code_tambien_se_libera_al_borrar(base_limpia):
    from django.utils import timezone

    _crear("Viejo", origina_code="COLMAGT6P1_X", deleted_at=timezone.now())

    assert _crear("Nuevo", origina_code="COLMAGT6P1_X").pk is not None


# ── Los dos juntos ──────────────────────────────────────────────────────────


def test_son_restricciones_independientes(base_limpia):
    """Compartir uno solo ya es conflicto: no hace falta que choquen los dos."""
    from django.db import IntegrityError

    _crear("Uno", codigo_tsf="COLAAA1", origina_code="COLAAA1_SITIO")

    with pytest.raises(IntegrityError):
        _crear("Dos", codigo_tsf="COLAAA1", origina_code="OTRO_DISTINTO")


def test_codigos_distintos_conviven(base_limpia):
    a = _crear("Uno", codigo_tsf="COLAAA1", origina_code="COLAAA1_SITIO")
    b = _crear("Dos", codigo_tsf="COLBBB2", origina_code="COLBBB2_OTRO")

    assert a.pk != b.pk


def test_las_dos_restricciones_estan_declaradas():
    """Si alguien las quita del modelo, la migración las borraría de la base."""
    from apps.proyectos.models import Proyecto

    nombres = {c.name for c in Proyecto._meta.constraints}

    assert "uq_proyectos_codigo_tsf_vivo" in nombres
    assert "uq_proyectos_origina_code_vivo" in nombres


def test_el_meta_no_vuelve_a_pisar_unique_together():
    """El `Meta` tenía CUATRO asignaciones seguidas a `unique_together`, y en
    Python cada una pisa a la anterior: tres restricciones parecían declaradas
    y no lo estaban. La base sí las tiene, pero el modelo no debe volver a
    escribirse así."""
    import re
    from pathlib import Path

    from apps.proyectos import models

    # Del ARCHIVO y no de `Proyecto.Meta`: Django reemplaza esa clase al
    # construir el modelo, y en tiempo de ejecucion apunta al `Meta` abstracto
    # de `Timer`. Lo que se quiere revisar es como esta escrito el codigo.
    fuente = Path(models.__file__).read_text(encoding="utf-8")
    i = fuente.index("class Proyecto(")
    bloque = fuente[i:fuente.index("\nclass ", i + 1)]

    assert len(re.findall(r"^\s+unique_together\s*=", bloque, re.M)) == 1
