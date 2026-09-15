"""`/proyectos/pendientes` deja de preguntarle a Quoia lo que ya sabe.

Para decidir si una frontera "ya genera" se le preguntaba a Quoia por **todas**
las del catálogo -- ~145, y cada una son hasta DOS llamadas (medidor de nodo, y
el CGM como respaldo). El filtro por nombre y el cruce contra nuestras fronteras
corrían DESPUÉS: se medía para botar.

Para toda frontera que YA está vinculada a un proyecto, la respuesta está en
`generacion_diaria`, que se indexa justo por `proyecto_id`: se le preguntaba a
un tercero algo que teníamos en casa.

Medido contra producción el 2026-09-15: de las 153 fronteras vivas con código,
las 153 están vinculadas. Ninguna suelta.

Ahora se filtra primero y se reparte:

    frontera vinculada      ->  `generacion_diaria`, una consulta para todas
    frontera desconocida    ->  Quoia, que es la única que la conoce
    excluida por nombre     ->  no se mide

Lo que NO cambia: el criterio. Sigue haciendo falta generación real (no basta
con que el medidor reporte), y un candidato que solo respalda Quoia sigue
exigiendo generación sostenida varios días.
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


@pytest.fixture(autouse=True)
def _caches_limpios():
    """Los cachés son variables de módulo: sin esto una prueba contamina a la
    siguiente."""
    from apps.proyectos.services import pendientes

    pendientes._generacion_real_cache.clear()
    pendientes._generacion_multidia_cache.clear()
    yield
    pendientes._generacion_real_cache.clear()
    pendientes._generacion_multidia_cache.clear()


def _border(nombre, frt_code, *, reporta=True, border_id=1):
    return {
        "name": nombre,
        "frt_generation": {
            "frt_code": frt_code,
            "last_report_date": "2026-09-14" if reporta else None,
            "id": border_id,
        },
    }


# ── El reparto ──────────────────────────────────────────────────────────────


def _repartir(borders, vinculadas):
    from apps.proyectos.services.pendientes import _repartir_borders

    return _repartir_borders(borders, vinculadas)


def test_una_frontera_vinculada_se_responde_desde_la_base():
    propias, ajenas = _repartir([_border("GD Bayunca", "frt001")], {"frt001": 7})

    assert propias == [("frt001", 7)]
    assert ajenas == [], "no hay que preguntarle a Quoia por algo que ya tenemos"


def test_una_frontera_que_no_tenemos_si_va_a_quoia():
    """El caso que justifica seguir llamando: una candidata de verdad."""
    propias, ajenas = _repartir([_border("GD Nueva", "frt999")], {})

    assert propias == []
    assert ajenas == [("frt999", "2026-09-14", 1)]


def test_las_excluidas_por_nombre_no_se_miden():
    """Se medían y después se botaban."""
    borders = [_border("Deprecated GD Vieja", "frt002"),
               _border("Solenium Piso 3", "frt003")]

    propias, ajenas = _repartir(borders, {})

    assert (propias, ajenas) == ([], [])


def test_la_que_nunca_reporto_no_se_mide():
    propias, ajenas = _repartir([_border("GD Muda", "frt004", reporta=False)], {})

    assert (propias, ajenas) == ([], [])


def test_tambien_vincula_por_el_codigo_de_consumo():
    """Una frontera de generación-consumo puede estar registrada por cualquiera
    de los dos códigos."""
    b = _border("GD Mixta", "frt005")
    b["frt_consumption"] = {"frt_code": "frt005c"}

    propias, ajenas = _repartir([b], {"frt005c": 12})

    assert propias == [("frt005", 12)]
    assert ajenas == []


def test_reparte_un_catalogo_mezclado():
    borders = [
        _border("GD Nuestra", "a"), _border("GD Ajena", "b"),
        _border("Deprecated X", "c"), _border("GD Muda", "d", reporta=False),
    ]

    propias, ajenas = _repartir(borders, {"a": 1})

    assert [c for c, _ in propias] == ["a"]
    assert [c for c, _, _ in ajenas] == ["b"]


# ── La respuesta que sale de nuestra base ───────────────────────────────────


def _proyecto(nombre="Planta"):
    from apps.proyectos.models import Proyecto

    return Proyecto.objects.create(nombre_comercial=nombre, estado="en_desarrollo")


def _generacion(proyecto_id, dias_atras, kwh=100):
    from datetime import timedelta

    from apps.plataforma.services.fechas import hoy_col
    from apps.proyectos.models import GeneracionDiaria

    GeneracionDiaria.objects.create(
        proyecto_id=proyecto_id, fecha=hoy_col() - timedelta(days=dias_atras),
        kwh_real=kwh, fuente="unergy",
    )


def _desde_la_base(propias):
    from apps.proyectos.services.pendientes import _generacion_desde_la_base

    return _generacion_desde_la_base(propias)


def test_sin_filas_no_genera(base_limpia):
    p = _proyecto()

    un_dia, sostenida = _desde_la_base([("frt001", p.id)])

    assert un_dia == {"frt001": False}
    assert sostenida == {"frt001": False}


def test_un_dia_suelto_cuenta_como_genero(base_limpia):
    p = _proyecto()
    _generacion(p.id, dias_atras=2)

    un_dia, sostenida = _desde_la_base([("frt001", p.id)])

    assert un_dia == {"frt001": True}
    assert sostenida == {"frt001": False}, "un día aislado no es sostenido"


def test_tres_dias_completos_seguidos_son_sostenida(base_limpia):
    p = _proyecto()
    for d in (1, 2, 3):
        _generacion(p.id, dias_atras=d)

    un_dia, sostenida = _desde_la_base([("frt001", p.id)])

    assert un_dia == {"frt001": True}
    assert sostenida == {"frt001": True}


def test_hoy_no_cuenta_para_la_sostenida(base_limpia):
    """Hoy puede estar parcial -- el mismo criterio que se le exigía a Quoia."""
    p = _proyecto()
    for d in (0, 1, 2):
        _generacion(p.id, dias_atras=d)

    _, sostenida = _desde_la_base([("frt001", p.id)])

    assert sostenida == {"frt001": False}, "falta el tercer día completo"


def test_un_dia_en_cero_rompe_la_racha(base_limpia):
    p = _proyecto()
    _generacion(p.id, dias_atras=1)
    _generacion(p.id, dias_atras=2, kwh=0)
    _generacion(p.id, dias_atras=3)

    _, sostenida = _desde_la_base([("frt001", p.id)])

    assert sostenida == {"frt001": False}


def test_resuelve_varias_fronteras_en_una_sola_consulta(base_limpia):
    from django.test.utils import CaptureQueriesContext
    from django.db import connection

    genera = _proyecto("Genera")
    no = _proyecto("No genera")
    _generacion(genera.id, dias_atras=1)

    propias = [("a", genera.id), ("b", no.id)]
    with CaptureQueriesContext(connection) as consultas:
        un_dia, _ = _desde_la_base(propias)

    assert un_dia == {"a": True, "b": False}
    assert len(consultas) == 1, f"una consulta para todas, no {len(consultas)}"


def test_sin_fronteras_propias_no_consulta(base_limpia):
    assert _desde_la_base([]) == ({}, {})


# ── El caché, que antes no tenía llave ──────────────────────────────────────


def test_el_cache_es_por_codigo_y_no_se_pisa_entre_llamadas():
    """El endpoint de diagnóstico pregunta por UNA frontera. Cuando el caché
    guardaba el diccionario entero de la última corrida, esa respuesta chica
    pisaba a la grande y todo lo que no estuviera ahí se leía como "no genera"
    -- un falso negativo callado durante una hora."""
    from apps.proyectos.services import pendientes

    pendientes._guardar(pendientes._generacion_real_cache, {"a": True, "b": True})
    pendientes._guardar(pendientes._generacion_real_cache, {"a": False})

    sabido, faltan = pendientes._vigentes(
        pendientes._generacion_real_cache, [("a", "", None), ("b", "", None)]
    )

    assert sabido == {"a": False, "b": True}, "la respuesta chica borró a la otra"
    assert faltan == []


def test_lo_que_no_esta_en_el_cache_se_pide():
    from apps.proyectos.services import pendientes

    pendientes._guardar(pendientes._generacion_real_cache, {"a": True})

    sabido, faltan = pendientes._vigentes(
        pendientes._generacion_real_cache, [("a", "", None), ("nuevo", "", None)]
    )

    assert sabido == {"a": True}
    assert [c for c, _, _ in faltan] == ["nuevo"]


def test_lo_vencido_se_vuelve_a_pedir(monkeypatch):
    from apps.proyectos.services import pendientes

    pendientes._guardar(pendientes._generacion_real_cache, {"a": True})
    avanzado = [0.0]
    original = pendientes.time.monotonic
    monkeypatch.setattr(
        pendientes.time, "monotonic",
        lambda: original() + pendientes._GENERACION_REAL_CACHE_TTL + 1,
    )

    sabido, faltan = pendientes._vigentes(
        pendientes._generacion_real_cache, [("a", "", None)]
    )

    assert sabido == {}
    assert [c for c, _, _ in faltan] == ["a"]


# ── Que el sync alimente esto de verdad ─────────────────────────────────────


def test_el_sync_no_filtra_por_estado():
    """Era el agujero: `generacion_diaria` solo se llenaba para proyectos ya
    marcados `en_operacion`, y tanto esta vista como "Próximos a energizar"
    preguntan justo por los que TODAVÍA NO lo están. La tabla nunca sabía de
    ellos y las dos vistas leían "no genera" para todos, en silencio."""
    import inspect

    from apps.proyectos.services import generacion_unergy

    fuente = inspect.getsource(generacion_unergy.sincronizar)

    assert 'estado="en_operacion"' not in fuente
    assert 'exclude(estado="cancelado")' in fuente
