"""El envío a Quoia corre en un hilo aparte y guarda cada fila apenas sale.

Caso real, 2026-09-29: con ~100 fronteras el `POST /enviar` pasaba del
`--timeout 120` de gunicorn (desde el 2026-09-04; con uvicorn no había límite).
El proceso moría a media lista: Generación, que va primero, llegaba a Quoia;
Consumo no. Y como el resultado se guardaba con un bulk_update AL FINAL, no
quedaba registro de nada -- `estado-quoia` daba total 0 con envíos ya hechos.

Lo que este archivo vigila:

  1. una fila enviada queda guardada aunque la corrida se caiga después,
  2. el hilo siempre deja un resultado y siempre libera la marca de "en curso",
  3. dos clics no lanzan dos envíos, y una caché caída no impide enviar,
  4. el contrato de `/enviar/estado` que el front consulta.

Sin base de datos (el repo no tiene `pytest-django`): los modelos, Quoia y la
caché se reemplazan por dobles.
"""
from types import SimpleNamespace

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

FECHA = None


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()

    global FECHA
    from datetime import date

    FECHA = date(2026, 9, 29)


@pytest.fixture
def cache_local():
    from django.core.cache import caches
    from django.test import override_settings

    with override_settings(CACHES={"default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-envio-background",
    }}):
        caches["default"].clear()
        yield caches["default"]
        caches["default"].clear()


class _CacheCaida:
    def add(self, *a, **kw):
        raise RuntimeError("redis no responde")


# ── 1. Cada fila se guarda al salir, no al final ─────────────────────────────

class _Fila:
    def __init__(self, nombre):
        self.nombre = nombre
        self.frontera = SimpleNamespace(codigo_frontera=f"frt{nombre}")
        self.enviado_quoia_en = None
        self.guardada_con = None

    def save(self, update_fields=None):
        self.guardada_con = update_fields


def _modelo(filas):
    class _Qs(list):
        def select_related(self, *a):
            return self

    return SimpleNamespace(objects=SimpleNamespace(filter=lambda **kw: _Qs(filas)))


def test_una_fila_enviada_queda_guardada_aunque_la_siguiente_reviente(monkeypatch):
    """El bug: la primera fila ya estaba en Quoia, pero sin registro, porque el
    guardado esperaba al final de la lista."""
    from datetime import datetime, timezone

    from apps.energia.services.reporte import envio

    primera, segunda = _Fila("a"), _Fila("b")
    monkeypatch.setattr(envio, "hay_pendientes", lambda fecha: False)
    monkeypatch.setattr(envio, "ReporteEnergiaGeneracion", _modelo([primera, segunda]))
    monkeypatch.setattr(envio, "ReporteEnergiaConsumo", _modelo([]))
    monkeypatch.setattr(envio, "GaiaClient", lambda: None)
    monkeypatch.setattr(envio, "resolver_borders", lambda gaia, codigos: {})

    def _enviar_a_quoia(rep, front, es_generacion, gaia, borders):
        if rep is segunda:
            raise RuntimeError("el proceso murió acá")
        rep.enviado_quoia_en = datetime.now(timezone.utc)
        return True, None

    monkeypatch.setattr(envio, "_enviar_a_quoia", _enviar_a_quoia)

    with pytest.raises(RuntimeError):
        envio.enviar(FECHA)

    assert primera.guardada_con == envio.CAMPOS_ENVIO


def test_una_fila_que_no_hacia_falta_enviar_no_se_guarda(monkeypatch):
    from apps.energia.services.reporte import envio

    fila = _Fila("a")
    monkeypatch.setattr(envio, "hay_pendientes", lambda fecha: False)
    monkeypatch.setattr(envio, "ReporteEnergiaGeneracion", _modelo([fila]))
    monkeypatch.setattr(envio, "ReporteEnergiaConsumo", _modelo([]))
    monkeypatch.setattr(envio, "GaiaClient", lambda: None)
    monkeypatch.setattr(envio, "resolver_borders", lambda gaia, codigos: {})
    monkeypatch.setattr(envio, "_enviar_a_quoia", lambda *a: (None, None))

    resultado = envio.enviar(FECHA)

    assert fila.guardada_con is None
    assert resultado["enviados"] == 0 and resultado["fallidos"] == []


# ── 2. El hilo siempre deja resultado y libera la marca ──────────────────────

def test_el_hilo_deja_el_resultado_y_libera_la_marca(cache_local, monkeypatch):
    from apps.energia.services.reporte import envio

    monkeypatch.setattr(envio, "enviar", lambda fecha: {
        "fecha": fecha, "enviados": 7, "fallidos": ["X — sin border_id en Quoia"],
        "bloqueado": False,
    })
    assert envio.tomar_envio(FECHA) is True

    envio.enviar_background(FECHA)

    assert envio.envio_en_curso(FECHA) is None
    ultimo = envio.ultimo_envio(FECHA)
    assert ultimo["enviados"] == 7
    assert ultimo["fallidos"] == ["X — sin border_id en Quoia"]
    assert ultimo["terminado_en"]


def test_si_el_envio_revienta_queda_el_error_y_se_libera_la_marca(cache_local, monkeypatch):
    """Sin esto el front esperaría para siempre, o hasta el TTL de la marca."""
    from apps.energia.services.reporte import envio

    def _revienta(fecha):
        raise RuntimeError("Quoia no responde")

    monkeypatch.setattr(envio, "enviar", _revienta)
    envio.tomar_envio(FECHA)

    envio.enviar_background(FECHA)

    assert envio.envio_en_curso(FECHA) is None
    assert envio.ultimo_envio(FECHA)["error_general"]


# ── 3. Un envío a la vez, y la caché caída no bloquea ────────────────────────

def test_dos_clics_no_lanzan_dos_envios(cache_local):
    from apps.energia.services.reporte import envio

    assert envio.tomar_envio(FECHA) is True
    assert envio.tomar_envio(FECHA) is False


def test_con_la_cache_caida_se_puede_enviar_igual(monkeypatch):
    from apps.energia.services.reporte import envio

    monkeypatch.setattr(envio, "cache", _CacheCaida())

    assert envio.tomar_envio(FECHA) is True


# ── 4. El contrato de /enviar/estado ─────────────────────────────────────────

def _estado():
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    from api.v1.reporte_energia.views import ReporteEnergiaViewSet

    peticion = Request(APIRequestFactory().get(f"/?fecha={FECHA}"))
    return ReporteEnergiaViewSet().enviar_estado(peticion).data


def test_estado_sin_envio_trae_fallidos_y_no_en_curso(cache_local):
    """El front hace `data.fallidos.length` sin preguntar, igual que en
    /ejecutar/estado."""
    data = _estado()

    assert data["fallidos"] == []
    assert data["en_curso"] is False


def test_estado_mientras_corre_dice_en_curso(cache_local):
    from apps.energia.services.reporte import envio

    envio.tomar_envio(FECHA)

    data = _estado()

    assert data["en_curso"] is True
    assert data["en_curso_desde"]


# ── 5. El simulacro no manda nada a Quoia ni escribe en la base ──────────────

class _GaiaQueNoEnvia:
    """Si el simulacro llegara a `post_report`, la prueba revienta."""

    def post_report(self, *a, **kw):
        raise AssertionError("el simulacro llamó a post_report: mandó datos a Quoia")


class _FilaQueNoSeGuarda(_Fila):
    def __init__(self, nombre, **campos):
        super().__init__(nombre)
        self.id = nombre
        self.frontera.id = nombre
        self.energia_final_kwh = 100.0
        self.caso = campos.get("caso", 5)
        self.medidor_usado = campos.get("medidor_usado", "principal")

    def save(self, *a, **kw):
        raise AssertionError("el simulacro guardó una fila en la base")


def _simular(monkeypatch, gen, con, borders):
    from apps.energia.services.reporte import envio

    monkeypatch.setattr(envio, "hay_pendientes", lambda fecha: False)
    monkeypatch.setattr(envio, "ReporteEnergiaGeneracion", _modelo(gen))
    monkeypatch.setattr(envio, "ReporteEnergiaConsumo", _modelo(con))
    monkeypatch.setattr(envio, "GaiaClient", _GaiaQueNoEnvia)
    monkeypatch.setattr(envio, "resolver_borders", lambda gaia, codigos: borders)
    monkeypatch.setattr(envio, "_nombre_frontera", lambda front: front.codigo_frontera)

    def _prohibido(*a, **kw):
        raise AssertionError("el simulacro llamó a _enviar_a_quoia")

    monkeypatch.setattr(envio, "_enviar_a_quoia", _prohibido)
    return envio.simular(FECHA)


def test_el_simulacro_no_envia_ni_guarda_y_clasifica_cada_fila(monkeypatch):
    gen = [_FilaQueNoSeGuarda("g1"), _FilaQueNoSeGuarda("g2", medidor_usado="cgm")]
    con = [
        # El caso del 2026-09-30: Consumo CGM corregido a mano -> SÍ sale.
        _FilaQueNoSeGuarda("c1", caso="CGM", medidor_usado="editado_manualmente"),
        _FilaQueNoSeGuarda("c2", caso="Medidor"),
    ]
    borders = {"frtg1": {"id": 1}, "frtg2": {"id": 2}, "frtc1": {"id": 3}}

    r = _simular(monkeypatch, gen, con, borders)

    assert r["simulacro"] is True
    assert [f["frontera_id"] for f in r["se_enviarian"]] == ["g1", "c1"]
    assert [f["frontera_id"] for f in r["se_saltarian"]] == ["g2"]
    assert [f["frontera_id"] for f in r["fallarian"]] == ["c2"]
    assert r["enviados"] == 0


def test_el_simulacro_no_tiene_un_camino_a_quoia_en_su_codigo():
    """Guard de lectura: `simular` no nombra la función que envía ni guarda.
    Si alguien lo cambia, que sea a propósito y con esta prueba enfrente."""
    import inspect

    from apps.energia.services.reporte import envio

    fuente = inspect.getsource(envio.simular).split('"""', 2)[-1]  # sin el docstring
    assert "_enviar_a_quoia" not in fuente
    assert "post_report" not in fuente
    assert ".save(" not in fuente
    assert "bulk_update" not in fuente
