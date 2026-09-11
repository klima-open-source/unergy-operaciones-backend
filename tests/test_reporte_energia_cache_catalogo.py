"""El catalogo de Quoia se cachea COMPARTIDO entre workers, no por proceso.

El detalle de una frontera del Reporte de Energia tarda en abrir, y una parte
del costo era este: los dos catalogos de Quoia --el mapa medidor→nodo y el
listado de fronteras-- se guardaban en variables globales del modulo.

Eso alcanzaba cuando `WORKERS` TENIA que ser 1 (el BackgroundScheduler vivia
dentro del proceso web). Al levantar esa restriccion, gunicorn corre con
`--workers 3` y cada worker quedo con su propia copia: abrir tres fronteras
seguidas caia tipicamente en tres procesos distintos y cada uno pagaba el fetch
completo (~5-9 s, medido en la auditoria CGM del 2026-08-26) por separado. El
TTL de 30 minutos daba una falsa sensacion de barato.

Redis ya estaba puesto para esto: `CACHES` en config/settings.py apunta al mismo
Redis del compose y su comentario dice, textualmente, que sirve para "estado
efimero (que es lo que era antes: memoria de un proceso)".

Lo que se prueba aca es lo que no se ve mirando el codigo: que un segundo
proceso NO vuelva a llamar a Quoia, que `usar_cache=False` siga trayendo fresco,
y --lo mas importante-- que si Redis no responde el panel siga funcionando. Un
catalogo de IDs no vale una caida.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


@pytest.fixture
def cache_local(monkeypatch):
    """Un cache de verdad, en memoria del test: hace de Redis.

    `locmem` y no el Redis real: la prueba tiene que correr en el CI, que no
    levanta Redis. Lo que importa es el comportamiento (compartido entre
    procesos), no el backend.
    """
    import django.core.cache as modulo_cache
    from django.core.cache.backends.locmem import LocMemCache

    # El codigo hace `from django.core.cache import cache` DENTRO de la funcion,
    # asi que reemplazar el atributo del modulo alcanza: no hace falta tocar la
    # configuracion de CACHES.
    falso = LocMemCache("prueba-cache-catalogo", {})
    monkeypatch.setattr(modulo_cache, "cache", falso, raising=False)
    falso.clear()
    yield falso
    falso.clear()


@pytest.fixture
def sin_cache_de_proceso():
    """Vacia el primer nivel: simula un worker recien arrancado."""
    from apps.energia.services.reporte import curvas

    curvas._local.clear()
    yield curvas
    curvas._local.clear()


class _GaiaFalso:
    """Cuenta cuantas veces se le pidio el catalogo a Quoia."""

    def __init__(self):
        self.nodos = 0
        self.borders = 0

    def get_all_nodes(self):
        self.nodos += 1
        return [{"id": 10, "meter": {"id": 1}}, {"id": 20, "meter": {"id": 2}}]

    def get_all_borders(self):
        self.borders += 1
        return [{"frt_generation": {"frt_code": "FRT001", "id": 7,
                                    "main_meter": 1, "backup_meter": 2}}]


def test_el_segundo_worker_no_vuelve_a_pedirle_a_quoia(cache_local, sin_cache_de_proceso):
    """El caso que motivo todo: tres workers, tres fetches completos."""
    curvas = sin_cache_de_proceso
    gaia = _GaiaFalso()

    curvas.construir_mapa_medidor_nodo(gaia)
    assert gaia.nodos == 1

    # Otro worker: mismo Redis, memoria de proceso vacia.
    curvas._local.clear()
    mapa = curvas.construir_mapa_medidor_nodo(gaia)

    assert gaia.nodos == 1, "el segundo worker volvio a llamar a Quoia"
    assert mapa == {1: 10, 2: 20}


def test_lo_mismo_para_el_catalogo_de_fronteras(cache_local, sin_cache_de_proceso):
    curvas = sin_cache_de_proceso
    gaia = _GaiaFalso()

    curvas.obtener_borders_crudos(gaia)
    curvas._local.clear()
    borders = curvas.obtener_borders_crudos(gaia)

    assert gaia.borders == 1
    assert borders[0]["frt_generation"]["frt_code"] == "FRT001"


def test_el_mapa_derivado_tampoco_repite_el_fetch(cache_local, sin_cache_de_proceso):
    """`construir_mapa_borders` deriva del catalogo crudo: comparte su cache."""
    curvas = sin_cache_de_proceso
    gaia = _GaiaFalso()

    curvas.construir_mapa_borders(gaia)
    curvas._local.clear()
    mapa = curvas.construir_mapa_borders(gaia)

    assert gaia.borders == 1
    assert mapa["frt001"]["main_meter"] == 1


def test_usar_cache_false_trae_fresco(cache_local, sin_cache_de_proceso):
    """La corrida diaria pide el catalogo sin cache, y tiene que seguir
    saltandose las dos capas."""
    curvas = sin_cache_de_proceso
    gaia = _GaiaFalso()

    curvas.construir_mapa_medidor_nodo(gaia)
    curvas.construir_mapa_medidor_nodo(gaia, usar_cache=False)

    assert gaia.nodos == 2


def test_el_primer_nivel_evita_ir_a_redis(cache_local, sin_cache_de_proceso):
    """Dentro del mismo worker no se deserializa el catalogo en cada apertura."""
    curvas = sin_cache_de_proceso
    gaia = _GaiaFalso()

    curvas.obtener_borders_crudos(gaia)
    cache_local.clear()          # si fuera a Redis, no encontraria nada
    curvas.obtener_borders_crudos(gaia)

    assert gaia.borders == 1, "no uso la memoria del proceso"


def test_si_redis_no_responde_el_panel_sigue_funcionando(sin_cache_de_proceso, monkeypatch):
    """Degradar, no caerse: sin Redis vuelve al cache por proceso."""
    curvas = sin_cache_de_proceso
    gaia = _GaiaFalso()

    class _CacheRoto:
        def get(self, *a, **k):
            raise ConnectionError("Redis caido")

        def set(self, *a, **k):
            raise ConnectionError("Redis caido")

    import django.core.cache as modulo_cache

    monkeypatch.setattr(modulo_cache, "cache", _CacheRoto(), raising=False)

    mapa = curvas.construir_mapa_medidor_nodo(gaia)
    assert mapa == {1: 10, 2: 20}

    # Y el primer nivel sigue sirviendo dentro del proceso.
    curvas.construir_mapa_medidor_nodo(gaia)
    assert gaia.nodos == 1
