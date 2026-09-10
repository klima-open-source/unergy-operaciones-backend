"""`manage.py revisar_potencias`: el reporte de potencias incoherentes.

Salio de consolidar la potencia AC en una sola columna (migracion 0005): al
comparar las dos tablas aparecieron 13 filas con la capacidad pico mal cargada
-- tres con 10.000 kWp para plantas de 990 kW AC, tres con 1 kWp, y dos donde la
pico es MENOR que la AC, que es fisicamente imposible.

El comando solo reporta, y eso es deliberado: una planta con AC 3.000 y pico
1.230 puede tener mal cualquiera de los dos numeros, y elegir es una decision de
operacion. Lo que se prueba aca es que la lista corta salga bien clasificada.
"""
from io import StringIO

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


def _planta(nombre, ac=None, pico=None):
    from apps.proyectos.models import Proyecto, ProyectoInfoTecnica

    proyecto = Proyecto.objects.create(nombre_comercial=nombre, potencia_ac_kw=ac)
    if pico is not None:
        ProyectoInfoTecnica.objects.create(
            proyecto=proyecto, capacidad_instalada_kwp=pico,
        )
    return proyecto


def _correr():
    from django.core.management import call_command

    salida = StringIO()
    call_command("revisar_potencias", stdout=salida, stderr=salida)
    return salida.getvalue()


def test_una_planta_sana_no_aparece(base_limpia):
    _planta("MGS 0012 La Reserva", ac=990, pico=1339.56)

    salida = _correr()

    assert "Sin incoherencias" in salida
    assert "La Reserva" not in salida


def test_marca_la_pico_menor_que_la_ac(base_limpia):
    """Bayunca en produccion: AC 3.000 y pico 1.230."""
    _planta("Bayunca", ac=3000, pico=1230)

    salida = _correr()

    assert "PICO MENOR QUE LA AC" in salida
    assert "Bayunca" in salida


def test_marca_el_cero_de_mas(base_limpia):
    """GD Delta 1 en produccion: 990 kW AC con 10.000 kWp cargados."""
    _planta("GD Delta 1", ac=990, pico=10000)

    salida = _correr()

    assert "RELACIÓN FUERA DE" in salida
    assert "GD Delta 1" in salida


def test_lista_las_que_no_tienen_pico(base_limpia):
    _planta("GD Garza", ac=990, pico=None)

    salida = _correr()

    assert "SIN CAPACIDAD PICO" in salida
    assert "GD Garza" in salida


def test_lista_las_que_no_tienen_ac(base_limpia):
    _planta("Sin Potencia", ac=None, pico=1200)

    salida = _correr()

    assert "SIN POTENCIA AC" in salida
    assert "Sin Potencia" in salida


def test_no_escribe_nada(base_limpia):
    """El comando es un reporte: las correcciones no se pueden adivinar."""
    from apps.proyectos.models import Proyecto, ProyectoInfoTecnica

    planta = _planta("Bayunca", ac=3000, pico=1230)

    _correr()

    assert float(Proyecto.objects.get(pk=planta.id).potencia_ac_kw) == 3000.0
    assert float(
        ProyectoInfoTecnica.objects.get(proyecto_id=planta.id).capacidad_instalada_kwp
    ) == 1230.0


def test_la_banda_sana_se_calcula_con_los_datos_de_la_base(base_limpia):
    """No se asume un rango: se reporta el de las plantas sanas que haya."""
    _planta("Una", ac=1000, pico=1300)
    _planta("Otra", ac=1000, pico=1500)

    salida = _correr()

    assert "Relación pico/AC sana:  1.30 a 1.50 (2 plantas)" in salida


def test_un_proyecto_borrado_no_cuenta(base_limpia):
    from django.utils import timezone

    from apps.proyectos.models import Proyecto

    planta = _planta("Borrada", ac=3000, pico=1230)
    Proyecto.objects.filter(pk=planta.id).update(deleted_at=timezone.now())

    salida = _correr()

    assert "Borrada" not in salida
