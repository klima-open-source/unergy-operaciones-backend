"""«Próximos a energizar» decide si una planta ya genera mirando nuestra base.

La vista devolvía **504: nunca terminaba de cargar** (reportado el 2026-09-15).

La lista sale de la base y es instantánea, pero después cruzaba con Quoia para
ocultar las plantas que YA generan aunque nadie lo haya confirmado: traía el
catálogo COMPLETO de borders y consultaba la generación real frontera por
frontera -- ~145 llamadas externas en cada carga. El caché existía, pero vivía
en una variable de módulo: cada worker de gunicorn tenía el suyo.

Peor: esa función estaba DUPLICADA. `/proximos-energizar` importaba la copia de
`app/services/proyectos_pendientes.py` --el árbol FastAPI apagado-- y
`/proyectos/pendientes` usaba la suya, cada una con su caché. El mismo trabajo se
hacía dos veces.

Ahora es **una consulta a `generacion_diaria`**. Esa tabla se volvió utilizable
el mismo día: su sync migró de Solenium (detenido desde el 6 de septiembre, 57
proyectos) a la API de Unergy (~90 proyectos, dos corridas diarias).

El dato puede tener hasta doce horas, y para esto da igual: una planta no arranca
y para dentro del mismo día.
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


def _proyecto(nombre="Planta"):
    from apps.proyectos.models import Proyecto

    return Proyecto.objects.create(nombre_comercial=nombre, estado="en_desarrollo")


def _generacion(proyecto, dias_atras, kwh=100):
    from datetime import timedelta

    from apps.plataforma.services.fechas import hoy_col
    from apps.proyectos.models import GeneracionDiaria

    return GeneracionDiaria.objects.create(
        proyecto_id=proyecto.id, fecha=hoy_col() - timedelta(days=dias_atras),
        kwh_real=kwh, fuente="unergy",
    )


def _generando(proyectos):
    from apps.proyectos.services.proximos_energizar import _generando_ya

    return _generando_ya(proyectos)


# ── La decisión ─────────────────────────────────────────────────────────────


def test_una_planta_con_generacion_reciente_cuenta(base_limpia):
    p = _proyecto()
    _generacion(p, dias_atras=1)

    assert _generando([p]) == {p.id}


def test_una_planta_sin_generacion_no_cuenta(base_limpia):
    """El caso normal: sigue en construcción, no ha arrancado."""
    p = _proyecto()

    assert _generando([p]) == set()


def test_la_generacion_vieja_no_cuenta(base_limpia):
    """Generó hace un mes y dejó de reportar: hoy no está generando."""
    p = _proyecto()
    _generacion(p, dias_atras=30)

    assert _generando([p]) == set()


def test_un_dia_en_cero_no_cuenta(base_limpia):
    """`kwh_real = 0` es "no generó", no "generó nada"."""
    p = _proyecto()
    _generacion(p, dias_atras=1, kwh=0)

    assert _generando([p]) == set()


def test_alcanza_con_un_dia_dentro_de_la_ventana(base_limpia):
    """No se exige generación sostenida: un día con energía ya dice que arrancó."""
    p = _proyecto()
    _generacion(p, dias_atras=6)

    assert _generando([p]) == {p.id}


def test_distingue_entre_proyectos(base_limpia):
    genera = _proyecto("Genera")
    no_genera = _proyecto("No Genera")
    _generacion(genera, dias_atras=2)

    assert _generando([genera, no_genera]) == {genera.id}


def test_sin_proyectos_no_consulta(base_limpia):
    assert _generando([]) == set()


def test_no_importa_de_que_fuente_venga_la_fila(base_limpia):
    """Lo que interesa es que haya generado, no quién lo reportó."""
    from datetime import timedelta

    from apps.plataforma.services.fechas import hoy_col
    from apps.proyectos.models import GeneracionDiaria

    p = _proyecto()
    GeneracionDiaria.objects.create(
        proyecto_id=p.id, fecha=hoy_col() - timedelta(days=1),
        kwh_real=50, fuente="manual",
    )

    assert _generando([p]) == {p.id}


# ── Que no quede nada del camino viejo ──────────────────────────────────────


def test_la_vista_ya_no_llama_a_ninguna_api_externa():
    """Era lo que la dejaba en 504."""
    from pathlib import Path

    from apps.proyectos.services import proximos_energizar

    fuente = Path(proximos_energizar.__file__).read_text(encoding="utf-8")

    for prohibido in ("GaiaClient", "get_all_borders", "_generacion_real_por_frt"):
        assert prohibido not in fuente, f"volvió a aparecer {prohibido}"


def test_no_depende_del_arbol_fastapi_apagado():
    """Importaba de `app/services/proyectos_pendientes.py`. `app/` se lee, no se
    extiende, y menos desde el camino caliente de una vista."""
    from pathlib import Path

    from apps.proyectos.services import proximos_energizar

    fuente = Path(proximos_energizar.__file__).read_text(encoding="utf-8")

    assert "from app." not in fuente
    assert "import app." not in fuente


def test_la_ventana_coincide_con_la_del_sync():
    """Mirar más atrás que lo que el sync puede llenar daría un hueco silencioso:
    plantas que generan y que la tabla todavía no conoce."""
    from apps.proyectos.services.generacion_unergy import DIAS_VENTANA
    from apps.proyectos.services.proximos_energizar import (
        DIAS_PARA_CONSIDERAR_GENERANDO,
    )

    assert DIAS_PARA_CONSIDERAR_GENERANDO <= DIAS_VENTANA
