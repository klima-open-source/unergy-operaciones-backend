"""`GET /fallas` acepta un rango sobre la fecha de identificación.

Faltaba, y el hueco costaba caro: el Histórico de Generación no tenía forma de
pedir "las fallas de agosto", así que se traía el historial COMPLETO --6.400
fallas en 33 peticiones, al abrir la pestaña y antes de que se eligiera nada--
para filtrarlo por fecha en el navegador.

Es un filtro más de los que ya existen, pero conviene no confundirlo con sus dos
vecinos, que preguntan cosas distintas sobre fechas distintas:

  · `fecha_programada_*` -- cuándo se PLANEÓ atenderla.
  · `activa_en_fecha`    -- qué estaba ABIERTO en un día dado (una falla de
                            enero sin resolver cuenta para marzo).
  · `fecha_identificacion_*` -- cuándo APARECIÓ, que es por lo que se mira un
                            período hacia atrás.

Los bordes son inclusivos en los dos extremos: quien pide "del 1 al 31 de
agosto" espera que el 1 y el 31 entren. Un `lt` en vez de `lte` perdería
silenciosamente el último día de cada consulta -- el tipo de error que nadie
nota hasta que cuadra un informe.
"""
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


def _obligatorios():
    """Lo que `Falla` exige y a estas pruebas no les interesa.

    `estado`, `prioridad`, `proyecto` y `registrado_por` son NOT NULL; se crean
    una vez por caso y se reusan, porque lo que se prueba es el filtro de
    fechas, no el modelo.
    """
    from apps.monitoreo.models import FallaCatEstado, FallaCatPrioridad
    from apps.plataforma.models import Usuario
    from apps.proyectos.models import Proyecto

    estado, _ = FallaCatEstado.objects.get_or_create(
        codigo="abierta", defaults={"etiqueta": "Abierta"})
    prioridad, _ = FallaCatPrioridad.objects.get_or_create(
        codigo="media", defaults={"etiqueta": "Media", "nivel": 2})
    proyecto, _ = Proyecto.objects.get_or_create(nombre_comercial="Planta Base")
    usuario, _ = Usuario.objects.get_or_create(
        email="pruebas@unergy.io", defaults={"nombre": "Pruebas"})
    return {
        "estado_id": estado.id, "prioridad_id": prioridad.id,
        "proyecto_id": proyecto.id, "registrado_por_id": usuario.id,
        "descripcion": "-",
    }


def _falla(codigo, fecha_identificacion, **kw):
    from apps.monitoreo.models import Falla

    campos = _obligatorios()
    campos.update(kw)
    return Falla.objects.create(
        codigo_interno=codigo,
        fecha_identificacion=fecha_identificacion,
        **campos,
    )


def _codigos(**params):
    from apps.monitoreo.services.fallas import consultas

    return sorted(f.codigo_interno for f in consultas.filtrar(params))


def test_el_rango_deja_fuera_lo_de_antes_y_lo_de_despues(base_limpia):
    _falla("ANTES", date(2026, 7, 31))
    _falla("DENTRO", date(2026, 8, 15))
    _falla("DESPUES", date(2026, 9, 1))

    assert _codigos(
        fecha_identificacion_desde=date(2026, 8, 1),
        fecha_identificacion_hasta=date(2026, 8, 31),
    ) == ["DENTRO"]


def test_el_primer_dia_del_rango_entra(base_limpia):
    """`gte`, no `gt`: quien pide desde el 1 de agosto espera el 1 de agosto."""
    _falla("PRIMER_DIA", date(2026, 8, 1))

    assert _codigos(fecha_identificacion_desde=date(2026, 8, 1)) == ["PRIMER_DIA"]


def test_el_ultimo_dia_del_rango_entra(base_limpia):
    """`lte`, no `lt`. Con `lt` se perdería el último día de CADA consulta, y
    eso no se nota hasta que alguien cuadra un informe."""
    _falla("ULTIMO_DIA", date(2026, 8, 31))

    assert _codigos(fecha_identificacion_hasta=date(2026, 8, 31)) == ["ULTIMO_DIA"]


def test_solo_desde_no_pone_techo(base_limpia):
    _falla("VIEJA", date(2026, 1, 1))
    _falla("NUEVA", date(2026, 12, 31))

    assert _codigos(fecha_identificacion_desde=date(2026, 6, 1)) == ["NUEVA"]


def test_solo_hasta_no_pone_piso(base_limpia):
    _falla("VIEJA", date(2026, 1, 1))
    _falla("NUEVA", date(2026, 12, 31))

    assert _codigos(fecha_identificacion_hasta=date(2026, 6, 1)) == ["VIEJA"]


def test_sin_el_filtro_no_cambia_nada(base_limpia):
    """El filtro es opcional: las demás vistas siguen viendo todo."""
    _falla("UNA", date(2026, 1, 1))
    _falla("OTRA", date(2026, 12, 31))

    assert _codigos() == ["OTRA", "UNA"]


def test_no_se_confunde_con_la_fecha_programada(base_limpia):
    """Dos fechas distintas de la misma falla: identificada en agosto, programada
    para octubre. El filtro tiene que mirar la de identificación."""
    _falla("AGOSTO", date(2026, 8, 10), fecha_programada=date(2026, 10, 5))

    assert _codigos(
        fecha_identificacion_desde=date(2026, 8, 1),
        fecha_identificacion_hasta=date(2026, 8, 31),
    ) == ["AGOSTO"]
    assert _codigos(
        fecha_identificacion_desde=date(2026, 10, 1),
        fecha_identificacion_hasta=date(2026, 10, 31),
    ) == []


def test_se_combina_con_los_demas_filtros(base_limpia):
    """Es el caso real del Histórico: un período Y unas plantas."""
    from apps.proyectos.models import Proyecto

    uno = Proyecto.objects.create(nombre_comercial="Planta Uno")
    dos = Proyecto.objects.create(nombre_comercial="Planta Dos")
    _falla("QUIERO", date(2026, 8, 10), proyecto_id=uno.id)
    _falla("OTRA_PLANTA", date(2026, 8, 11), proyecto_id=dos.id)
    _falla("OTRO_MES", date(2026, 9, 10), proyecto_id=uno.id)

    assert _codigos(
        proyecto_id=uno.id,
        fecha_identificacion_desde=date(2026, 8, 1),
        fecha_identificacion_hasta=date(2026, 8, 31),
    ) == ["QUIERO"]


def test_el_endpoint_lee_los_dos_parametros():
    """El contrato con el frontend: que `list()` los pase al filtro. Sin esto el
    filtro existe y nadie lo puede usar."""
    import inspect

    from api.v1.fallas.views import FallaViewSet

    fuente = inspect.getsource(FallaViewSet.list)

    assert '"fecha_identificacion_desde"' in fuente
    assert '"fecha_identificacion_hasta"' in fuente
    # Por `par.fecha` y no `query_params.get`: una fecha mal formada tiene que
    # salir como 422, no como un 500 del ORM.
    assert 'par.fecha(\n                request, "fecha_identificacion_desde")' in fuente
