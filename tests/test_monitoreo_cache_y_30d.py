"""Las dos optimizaciones del monitoreo solar: el caché compartido y los 30 días.

**El caché.** Era un `dict` de módulo con una nota que decía que servía "mientras
el despliegue corra con un solo proceso web (WORKERS=1)". Hace rato que no: el
compose levanta `gunicorn --workers 3`. O sea que había tres cachés que no se
conocían entre sí, y una de cada tres recargas caía en un proceso frío y pagaba
las ~235 llamadas externas enteras. Para quien mira la pantalla eso es "a veces
carga al instante y a veces se demora", sin patrón que explique cuándo.

Lo que se fija acá no es que use Redis (eso es el cómo), sino las dos propiedades
que tiene que cumplir al compartirlo:

  · que lo que guarda un proceso lo lea otro, y
  · que si Redis no está, no se rompa nada — un panel de monitoreo no se cae
    porque el caché no esté, se pone lento, que es lo que hacía antes.

Y una tercera que es fácil de romper sin darse cuenta: el TTL que queda **no se
reinicia** al leerlo de Redis. Si se reiniciara, una entrada a punto de vencer
viviría un TTL completo más en el proceso que la leyó, y el dato podría llegar
a pantalla con hasta el doble de la antigüedad que promete `CACHE_TTL_*`. En una
vista que se llama "en vivo", eso importa.

**Los 30 días.** `generation_30d` cuesta una llamada externa por tarjeta y
ninguna vista la dibuja. El flag la apaga, pero viene en `True`: este dato el
endpoint ya lo servía, y quién más lo consume no se puede ver desde este
repositorio. Un flag que hay que pedir para seguir recibiendo lo de siempre
rompe callado al que no se entere.
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
def sv():
    """El módulo con el caché de proceso vacío y un Redis de mentira."""
    from django.test import override_settings

    from apps.energia.services import solarview_monitoreo

    solarview_monitoreo._cache.clear()
    with override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    ):
        from django.core.cache import cache

        cache.clear()
        yield solarview_monitoreo
    solarview_monitoreo._cache.clear()


# ── El caché compartido ─────────────────────────────────────────────────────


def test_lo_que_guarda_un_proceso_lo_lee_otro(sv):
    """Es el punto de todo el cambio: `_cache` local vacío (= proceso recién
    arrancado, o uno de los otros dos workers) y el dato igual aparece."""
    sv._cache_set("flota", 60, {"filas": [1, 2, 3]})

    sv._cache.clear()  # otro worker: no vio nada de lo anterior

    assert sv._cache_get("flota") == {"filas": [1, 2, 3]}


def test_lo_guardado_queda_tambien_en_el_proceso(sv):
    """El nivel local se conserva porque es gratis y ahorra el viaje a Redis
    dentro de la misma request."""
    sv._cache_set("flota", 60, {"x": 1})

    assert sv._cache["flota"][2] == {"x": 1}


def test_una_clave_que_nadie_guardo_es_none(sv):
    assert sv._cache_get("no existe") is None


def test_leerlo_de_redis_no_reinicia_el_ttl(sv, monkeypatch):
    """La propiedad frágil: al traerlo de Redis, al dato le queda lo que le
    quedaba, no un TTL entero de nuevo."""
    import time

    ahora = [1000.0]
    monkeypatch.setattr(time, "time", lambda: ahora[0])
    sv._cache_set("flota", 60, {"x": 1})

    ahora[0] += 50          # le quedan 10 de los 60
    sv._cache.clear()
    assert sv._cache_get("flota") == {"x": 1}

    ttl_local = sv._cache["flota"][1]
    assert 9 <= ttl_local <= 11, f"el TTL se reinició: quedó en {ttl_local}"


def test_lo_vencido_en_redis_no_se_devuelve(sv, monkeypatch):
    import time

    ahora = [1000.0]
    monkeypatch.setattr(time, "time", lambda: ahora[0])
    sv._cache_set("flota", 60, {"x": 1})

    ahora[0] += 61
    sv._cache.clear()

    assert sv._cache_get("flota") is None


def test_el_cache_local_sigue_venciendo(sv, monkeypatch):
    """Sin Redis de por medio: el TTL del nivel de proceso es el de antes."""
    reloj = [1000.0]
    monkeypatch.setattr(sv.time, "monotonic", lambda: reloj[0])

    sv._cache["solo_local"] = (reloj[0], 60, {"x": 1})
    reloj[0] += 61

    assert sv._cache_get("solo_local") is None


def test_sin_redis_no_se_rompe_nada(sv, monkeypatch):
    """Redis caído: `_cache_set` no explota y el nivel de proceso sigue
    sirviendo. Es el comportamiento que había antes de este cambio."""
    from django.core.cache import cache

    def _reventar(*a, **kw):
        raise RuntimeError("Redis no responde")

    monkeypatch.setattr(cache, "set", _reventar)
    monkeypatch.setattr(cache, "get", _reventar)

    sv._cache_set("flota", 60, {"x": 1})

    assert sv._cache_get("flota") == {"x": 1}, "el cache por proceso debía seguir"


def test_sin_redis_un_proceso_frio_devuelve_none_y_no_explota(sv, monkeypatch):
    from django.core.cache import cache

    def _reventar(*a, **kw):
        raise RuntimeError("Redis no responde")

    monkeypatch.setattr(cache, "get", _reventar)
    sv._cache.clear()

    assert sv._cache_get("flota") is None


def test_las_claves_van_bajo_su_propio_prefijo(sv):
    """Comparte el Redis con el resto de la plataforma; sin prefijo una clave
    como `detail:12:2026-09-11` es fácil de chocar."""
    from django.core.cache import cache

    sv._cache_set("detail:12", 60, {"x": 1})

    assert cache.get("detail:12") is None
    assert cache.get(sv._PREFIJO_REDIS + "detail:12") is not None


# ── Los 30 días ─────────────────────────────────────────────────────────────


def test_los_30_dias_vienen_prendidos(sv):
    """Al revés que `incluir_snapshot`. El endpoint ya mandaba estos campos, y
    la app móvil vive en otro repositorio: apagarlos por defecto rompería
    callado a quien no se entere."""
    import inspect

    firma = inspect.signature(sv.monitoreo_detalle)

    assert firma.parameters["incluir_30d"].default is True
    assert firma.parameters["incluir_snapshot"].default is False


def test_apagados_no_se_pide_la_serie(sv):
    """Lo que se ahorra: la llamada a `get_energy`, una por tarjeta."""
    import inspect

    fuente = inspect.getsource(sv.monitoreo_detalle)

    assert "if (sol_id and incluir_30d) else None" in fuente


def test_la_clave_del_cache_distingue_las_dos_formas(sv):
    """Si no las distinguiera, la primera respuesta sin 30 días se le serviría
    a quien SÍ los pidió (o al revés) durante los 90 segundos del TTL."""
    import inspect

    fuente = inspect.getsource(sv.monitoreo_detalle)

    assert "{int(incluir_snapshot)}:{int(incluir_30d)}" in fuente


# ── La bandera del endpoint ─────────────────────────────────────────────────


def _request(**params):
    from types import SimpleNamespace

    return SimpleNamespace(query_params=params)


def _bandera(valor, por_defecto):
    from api.v1.generacion_solar.views import _bandera as f

    peticion = _request() if valor is None else _request(flag=valor)
    return f(peticion, "flag", por_defecto=por_defecto)


@pytest.mark.parametrize("valor", ["1", "true", "TRUE", "yes", "si", "sí"])
def test_se_puede_prender(valor):
    assert _bandera(valor, por_defecto=False) is True


@pytest.mark.parametrize("valor", ["0", "false", "FALSE", "no"])
def test_se_puede_apagar(valor):
    """El sentido que faltaba: antes solo se leía `=1`, así que un flag que
    viene prendido no se podía apagar desde la query."""
    assert _bandera(valor, por_defecto=True) is False


def test_sin_el_parametro_manda_el_default():
    assert _bandera(None, por_defecto=True) is True
    assert _bandera(None, por_defecto=False) is False


def test_un_valor_raro_deja_el_default():
    """Son optimizaciones, no argumentos de negocio: un typo no vale un 400 en
    una pantalla de monitoreo."""
    assert _bandera("puede ser", por_defecto=True) is True
    assert _bandera("puede ser", por_defecto=False) is False
    assert _bandera("", por_defecto=True) is True


def test_el_endpoint_pasa_los_dos_flags():
    import inspect

    from api.v1.generacion_solar.views import GeneracionSolarViewSet

    fuente = inspect.getsource(GeneracionSolarViewSet.monitoring_detalle)

    assert 'incluir_snapshot=_bandera(request, "incluir_snapshot", por_defecto=False)' in fuente
    assert 'incluir_30d=_bandera(request, "incluir_30d", por_defecto=True)' in fuente
