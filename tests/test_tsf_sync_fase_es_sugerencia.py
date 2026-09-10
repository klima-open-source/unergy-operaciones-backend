"""La fase de construcción de un proyecto existente se PROPONE, no se aplica.

Cambiar la fase de un proyecto que ya existe es una actualización, y desde el
2026-09-10 las actualizaciones que vienen de Sun Factory se proponen en
`/proyectos/pendientes` para que una persona las confirme, en vez de aplicarse
solas cada 6 horas.

Lo llamativo del bug que esto cierra: `resolver_pendientes` YA sabía proponer la
fase --incluida la guarda de no devolver a una fase anterior un proyecto ya
energizado, que salió de un caso real (Chima Oriente, Chiriguaná N1 y Valencia
Oriente 1, 2026-07-09)-- pero esa sugerencia casi nunca alcanzaba a verse: la
tarea de sincronización había aplicado el cambio antes de que nadie abriera la
pantalla. La cola de sugerencias existía y estaba vacía por construcción.

Lo que la sincronización SIGUE haciendo sola, y por qué:

  - **Rellenar huecos** (municipio, coordenadas, potencia, códigos): no pisa
    nada, y sin eso un proyecto recién enlazado queda sin datos que la fuente sí
    tiene.
  - **`avance_obra_pct`**: es una medición viva; pedir confirmación cada 6 horas
    para mover una barra de progreso sería trabajo sin decisión.
  - **`fecha_estimada_energizacion`**: se queda automática por costo. Vive en los
    milestones de Sun Factory, que son UNA llamada por proyecto: llevarla a la
    cola significaría enriquecer cada candidato cada vez que alguien abre la
    pantalla de pendientes.
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


def _proyecto(**kw):
    from apps.proyectos.models import Proyecto

    kw.setdefault("nombre_comercial", "Morroa Sur")
    kw.setdefault("origina_code", "COLSUCT3P1_MORROA_SUR")
    kw.setdefault("codigo_tsf", "COLSUCT3P1")
    kw.setdefault("sunfactory_project_id", "106")
    kw.setdefault("estado", "en_desarrollo")
    return Proyecto.objects.create(**kw)


def _payload(**kw):
    from datetime import date

    fila = {
        "origina_code": "COLSUCT3P1_MORROA_SUR",
        "base_name": "COLSUCT3P1_MORROA_SUR",
        "tsf_code": "COLSUCT3P1",
        "commercial_name": "Morroa Sur",
        "status": "Próximo a energizar",
        "stage": "deploy",
        "energization_date": date(2026, 9, 1),
        "energization_source": "sunfactory",
        "avance_pct": 88.5,
        "monthly_mwh": 127.71,
        "installed_power_kwp": 990,
        "already_generating": False,
        "municipio": "Morroa",
        "departamento": "Sucre",
        "latitud": None,
        "longitud": None,
        "solenium_id": 106,
    }
    fila.update(kw)
    return [fila]


def _sincronizar(monkeypatch, filas):
    from apps.proyectos.services import tsf_sync

    monkeypatch.setattr(
        tsf_sync, "fetch_sunfactory_projects", lambda **k: (filas, []),
    )
    return tsf_sync.sync_tsf_projects()


def test_la_sincronizacion_no_cambia_la_fase(base_limpia, monkeypatch):
    """Sun Factory dice "próximo a energizar" y el proyecto está "en
    construcción": antes la tarea lo cambiaba sola."""
    from apps.proyectos.models import Proyecto

    proyecto = _proyecto(fase_construccion="en_construccion")

    _sincronizar(monkeypatch, _payload(status="Próximo a energizar"))

    assert Proyecto.objects.get(pk=proyecto.id).fase_construccion == "en_construccion"


def test_tampoco_la_pone_cuando_esta_vacia(base_limpia, monkeypatch):
    """Ni siquiera rellenando: la fase de un proyecto que ya existe es una
    decisión, y la cola de pendientes es donde se toma."""
    from apps.proyectos.models import Proyecto

    proyecto = _proyecto(fase_construccion=None)

    _sincronizar(monkeypatch, _payload())

    assert Proyecto.objects.get(pk=proyecto.id).fase_construccion is None


def test_lo_que_si_sigue_sincronizando(base_limpia, monkeypatch):
    """El resto no cambia: rellena huecos, actualiza el avance y la fecha
    estimada."""
    from datetime import date

    from apps.proyectos.models import Proyecto

    proyecto = _proyecto(municipio=None, avance_obra_pct=40)

    _sincronizar(monkeypatch, _payload(avance_pct=88.5))

    guardado = Proyecto.objects.get(pk=proyecto.id)
    assert guardado.municipio == "Morroa"
    assert float(guardado.avance_obra_pct) == 88.5
    assert guardado.fecha_estimada_energizacion == date(2026, 9, 1)


def test_el_proyecto_se_sigue_contando_como_actualizado(base_limpia, monkeypatch):
    proyecto = _proyecto(municipio=None)

    stats = _sincronizar(monkeypatch, _payload())

    assert stats["actualizados"] == 1
    assert proyecto.id
