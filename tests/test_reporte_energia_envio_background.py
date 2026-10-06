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
  4. el contrato de `/enviar/estado` que el front consulta,
  5. el resumen del envío: cada frontera en una sola casilla, y en vivo.

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

def _estado(monkeypatch):
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    from api.v1.reporte_energia.views import ReporteEnergiaViewSet

    from apps.energia.services.reporte import envio

    # El resumen lee las filas del día; acá solo importa que viaje.
    monkeypatch.setattr(envio, "resumen_envio", lambda fecha: {"total": 0})
    peticion = Request(APIRequestFactory().get(f"/?fecha={FECHA}"))
    return ReporteEnergiaViewSet().enviar_estado(peticion).data


def test_estado_sin_envio_trae_fallidos_y_no_en_curso(cache_local, monkeypatch):
    """El front hace `data.fallidos.length` sin preguntar, igual que en
    /ejecutar/estado."""
    data = _estado(monkeypatch)

    assert data["fallidos"] == []
    assert data["en_curso"] is False
    assert data["resumen"] == {"total": 0}


def test_estado_mientras_corre_dice_en_curso(cache_local, monkeypatch):
    from apps.energia.services.reporte import envio

    envio.tomar_envio(FECHA)

    data = _estado(monkeypatch)

    assert data["en_curso"] is True
    assert data["en_curso_desde"]


# ── 5. El resumen del envío ──────────────────────────────────────────────────

def _fila_resumen(nombre, medidor_usado="principal", caso=2, enviado_en=None, ok=None, error=None):
    return SimpleNamespace(
        frontera=SimpleNamespace(id=hash(nombre) % 1000, nombre=nombre),
        medidor_usado=medidor_usado, caso=caso,
        enviado_quoia_en=enviado_en, enviado_quoia_ok=ok, enviado_quoia_error=error,
    )


def _resumen(monkeypatch, gen, con=()):
    from apps.energia.services.reporte import envio

    monkeypatch.setattr(envio, "ReporteEnergiaGeneracion", _modelo(list(gen)))
    monkeypatch.setattr(envio, "ReporteEnergiaConsumo", _modelo(list(con)))
    monkeypatch.setattr(envio, "_nombre_frontera", lambda front: front.nombre)
    return envio.resumen_envio(FECHA)


def test_resumen_pone_cada_frontera_en_una_sola_casilla(cache_local, monkeypatch):
    from datetime import datetime, timezone

    ayer = datetime(2026, 9, 30, tzinfo=timezone.utc)
    data = _resumen(monkeypatch, gen=[
        _fila_resumen("enviada", enviado_en=ayer, ok=True),
        _fila_resumen("fallida", enviado_en=ayer, ok=False, error="Quoia rechazó el envío"),
        _fila_resumen("sin enviar"),
        _fila_resumen("cgm gen", medidor_usado="cgm", caso=1),
        _fila_resumen("excluida", medidor_usado="excluida"),
    ], con=[
        _fila_resumen("cgm con", medidor_usado="cgm", caso="CGM"),
    ])

    assert data["total"] == 6
    assert (data["enviadas"], data["fallidas"], data["por_enviar"]) == (1, 1, 1)
    assert (data["automaticas"], data["excluidas"]) == (2, 1)
    assert data["fallidas_detalle"] == [{
        "frontera_id": hash("fallida") % 1000, "nombre_proyecto": "fallida",
        "tipo": "generacion", "motivo": "Quoia rechazó el envío",
    }]


def test_resumen_en_vivo_lo_de_un_envio_anterior_cuenta_como_por_enviar(cache_local, monkeypatch):
    """Mientras corre un reenvío, una fila enviada AYER todavía no salió en
    esta corrida: contarla como enviada adelantaría el avance."""
    from datetime import datetime, timedelta, timezone

    from apps.energia.services.reporte import envio

    envio.tomar_envio(FECHA)
    inicio = datetime.fromisoformat(envio.envio_en_curso(FECHA)["desde"])
    data = _resumen(monkeypatch, gen=[
        _fila_resumen("de ayer", enviado_en=inicio - timedelta(days=1), ok=True),
        _fila_resumen("de ahora", enviado_en=inicio + timedelta(seconds=5), ok=True),
    ])

    assert (data["enviadas"], data["por_enviar"]) == (1, 1)
