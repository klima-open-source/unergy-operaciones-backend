"""Los estados de los reconectadores: caché compartida y última lectura buena.

Lo que se fija:

  · una segunda consulta dentro del minuto no vuelve a SolarView;
  · un 404 es "no tiene relay" y el proyecto no sale;
  · un ERROR no lo hace desaparecer: sale su última lectura buena marcada
    `lectura_fallida`, y el listado se guarda menos tiempo para reintentar;
  · tras un comando, `olvidar_estados` hace que la próxima consulta lea de nuevo.

Ningún test sale a la red.
"""
from types import SimpleNamespace

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


@pytest.fixture
def relays(monkeypatch):
    from django.test import override_settings

    from apps.energia.services import solarview_monitoreo
    from apps.monitoreo.services import reconectadores

    solarview_monitoreo._cache.clear()
    with override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    ):
        from django.core.cache import cache

        cache.clear()
        yield reconectadores
    solarview_monitoreo._cache.clear()


def _p(pid, sv):
    return SimpleNamespace(id=pid, nombre_comercial=f"Planta {pid}", project_id_solarview=str(sv))


def _respuestas(monkeypatch, relays, por_sv):
    """`por_sv`: {sv_id: ("ok", medidas) | ("sin_relay", {}) | ("error", {})}."""
    llamadas = []

    def leer(sv_id):
        llamadas.append(sv_id)
        return por_sv[sv_id]

    monkeypatch.setattr(relays, "_leer", leer)
    return llamadas


def test_la_segunda_consulta_sale_del_cache(monkeypatch, relays):
    llamadas = _respuestas(monkeypatch, relays, {10: ("ok", {"active": True})})

    primero = relays.estados_de([_p(1, 10)])
    segundo = relays.estados_de([_p(1, 10)])

    assert primero == segundo
    assert primero[0]["active"] is True and primero[0]["lectura_fallida"] is False
    assert llamadas == [10]


def test_sin_relay_no_sale(monkeypatch, relays):
    _respuestas(monkeypatch, relays, {10: ("sin_relay", {})})

    assert relays.estados_de([_p(1, 10)]) == []


def test_un_error_muestra_la_ultima_lectura_buena(monkeypatch, relays):
    _respuestas(monkeypatch, relays, {10: ("ok", {"active": True, "kw": 500})})
    relays.estados_de([_p(1, 10)])
    relays.olvidar_estados()

    _respuestas(monkeypatch, relays, {10: ("error", {})})
    ttls = []
    original = relays._guardar_cache
    monkeypatch.setattr(relays, "_guardar_cache",
                        lambda k, ttl, d: ttls.append((k, ttl)) or original(k, ttl, d))

    [estado] = relays.estados_de([_p(1, 10)])

    assert estado["active"] is True and estado["potencia_kw"] == 500
    assert estado["lectura_fallida"] is True
    assert (relays.CLAVE_ESTADOS, relays.CACHE_TTL_ESTADOS_CON_ERROR) in ttls


def test_un_error_sin_lectura_previa_no_inventa_nada(monkeypatch, relays):
    _respuestas(monkeypatch, relays, {10: ("error", {})})

    assert relays.estados_de([_p(1, 10)]) == []


def test_olvidar_estados_obliga_a_leer_de_nuevo(monkeypatch, relays):
    llamadas = _respuestas(monkeypatch, relays, {10: ("ok", {"active": False})})

    relays.estados_de([_p(1, 10)])
    relays.olvidar_estados()
    relays.estados_de([_p(1, 10)])

    assert llamadas == [10, 10]


def test_si_otro_proceso_esta_consultando_devuelve_lo_ultimo_al_instante(monkeypatch, relays):
    _respuestas(monkeypatch, relays, {10: ("ok", {"active": True})})
    relays.estados_de([_p(1, 10)])
    relays.olvidar_estados()

    llamadas = _respuestas(monkeypatch, relays, {10: ("ok", {"active": False})})
    monkeypatch.setattr(relays, "_tomar_candado", lambda: False)

    [estado] = relays.estados_de([_p(1, 10)])

    assert estado["active"] is True  # la última conocida, sin ir a SolarView
    assert estado["lectura_fallida"] is False  # es de hace segundos
    assert llamadas == []


def test_mientras_otro_consulta_una_lista_vieja_sale_marcada(monkeypatch, relays):
    _respuestas(monkeypatch, relays, {10: ("ok", {"active": True})})
    relays.estados_de([_p(1, 10)])
    relays.olvidar_estados()

    llamadas = _respuestas(monkeypatch, relays, {10: ("ok", {"active": False})})
    monkeypatch.setattr(relays, "_tomar_candado", lambda: False)
    # Nadie abrió la pantalla en una hora: la última lista ya no es de ahora.
    ahora = relays.time.time()
    monkeypatch.setattr(relays.time, "time", lambda: ahora + 3600)

    [estado] = relays.estados_de([_p(1, 10)])

    assert estado["active"] is True and estado["lectura_fallida"] is True
    assert llamadas == []


def test_mientras_otro_consulta_sin_lista_previa_no_inventa_nada(monkeypatch, relays):
    llamadas = _respuestas(monkeypatch, relays, {10: ("ok", {"active": True})})
    monkeypatch.setattr(relays, "_tomar_candado", lambda: False)

    assert relays.estados_de([_p(1, 10)]) == []
    assert llamadas == []


def test_nada_queda_en_la_memoria_del_proceso(monkeypatch, relays):
    """Con varios procesos de gunicorn, una copia por proceso se desactualiza y
    pisa la de Redis. Todo va al caché compartido."""
    from django.core.cache import cache

    from apps.energia.services import solarview_monitoreo

    _respuestas(monkeypatch, relays, {10: ("ok", {"active": True})})
    relays.estados_de([_p(1, 10)])

    assert solarview_monitoreo._cache == {}
    assert cache.get(relays.CLAVE_ESTADOS) is not None
    assert cache.get(relays.CLAVE_ULTIMO) is not None
    assert cache.get(relays.CLAVE_ULTIMA_LISTA) is not None


def test_la_ultima_lectura_es_la_del_cache_compartido(monkeypatch, relays):
    """Si otro proceso dejó en Redis una lectura más nueva, es la que se usa
    cuando la de ahora falla."""
    from django.core.cache import cache

    _respuestas(monkeypatch, relays, {10: ("ok", {"active": True})})
    relays.estados_de([_p(1, 10)])
    relays.olvidar_estados()
    # Otro proceso leyó después: el relay quedó en OFF.
    ultimo = cache.get(relays.CLAVE_ULTIMO)
    ultimo["1"] = {**ultimo["1"], "active": False}
    cache.set(relays.CLAVE_ULTIMO, ultimo, relays.CACHE_TTL_ULTIMO)

    _respuestas(monkeypatch, relays, {10: ("error", {})})
    [estado] = relays.estados_de([_p(1, 10)])

    assert estado["active"] is False and estado["lectura_fallida"] is True


def test_la_lectura_del_relay_es_corta_y_de_un_intento(monkeypatch, relays):
    from apps.comun.integraciones import solarview_client

    pedidos = []
    monkeypatch.setattr(
        solarview_client.SolarViewClient, "_get_con_estado",
        lambda self, url, params=None, **kw: pedidos.append((url, params, kw)) or ("error", None),
    )
    monkeypatch.setenv("SOLARVIEW_TOKEN", "tok")
    monkeypatch.setenv("SOLARVIEW_BASE_URL", "https://api.sole.tech")
    c = solarview_client.SolarViewClient()
    monkeypatch.setattr(relays, "cliente", lambda: c)

    assert relays._leer(17) == ("error", {})
    assert pedidos == [(
        "https://api.sole.tech/solarview/config/recloser/", {"project_id": 17},
        {"timeout": solarview_client.TIMEOUT_EN_PANTALLA, "intentos": 1},
    )]
    assert solarview_client.TIMEOUT_EN_PANTALLA <= 10
