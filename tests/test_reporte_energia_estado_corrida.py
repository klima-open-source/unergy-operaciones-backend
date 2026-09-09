"""El estado de la corrida del Reporte de Energia vive en la cache compartida.

Antes eran dos diccionarios en memoria del proceso (`_ULTIMAS_CORRIDAS` y
`_CANCELAR` en `orquestador.py`). El compose arranca gunicorn con
`--workers ${WORKERS:-3}`: el `POST /ejecutar` corria en un worker y el
`POST /ejecutar/cancelar` caia en cualquiera de los tres, asi que "Detener"
respondia `{"solicitado": true}` sin detener nada. Y el resultado de la corrida
automatica de las 3:30 quedaba en la memoria del contenedor de Celery, donde
`GET /ejecutar/estado` no podia verlo ni por casualidad.

**Lo que este archivo vigila de verdad es que la corrida automatica no se
volviera fragil con el cambio.** Es la unica razon por la que el alcance quedo
partido en dos, y es lo que no se puede verificar leyendo el diff:

  1. una cache caida no puede tumbar una corrida (todo falla hacia "seguir"),
  2. la corrida automatica nunca cede el paso ni se puede detener.

Lo que NO se prueba aca es el bucle de `ejecutar_dia` frontera por frontera:
necesita base de datos y el repo no tiene `pytest-django` (ver el docstring de
test_nombres_definidos.py). La verificacion de que "Detener" para una corrida
manual de verdad es manual, en el servidor.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

FECHA = None  # se llena en _django_listo: `date` no se importa antes de setup()


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()

    global FECHA
    from datetime import date

    FECHA = date(2026, 9, 8)


@pytest.fixture
def cache_local():
    """Cache de memoria en vez del Redis real -- alcanza para lo que se prueba
    aca (que el valor se guarde y se lea con la misma clave). Lo que la de
    memoria NO reproduce es el aislamiento entre procesos, que es justo el bug
    original y solo se ve en el servidor."""
    from django.core.cache import caches
    from django.test import override_settings

    with override_settings(CACHES={"default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-estado-corrida",
    }}):
        caches["default"].clear()
        yield caches["default"]
        caches["default"].clear()


class _CacheCaida:
    """Una cache que lanza en todo -- Redis apagado, timeout, lo que sea."""

    def get(self, *a, **kw):
        raise RuntimeError("redis no responde")

    def set(self, *a, **kw):
        raise RuntimeError("redis no responde")

    def add(self, *a, **kw):
        raise RuntimeError("redis no responde")

    def delete(self, *a, **kw):
        raise RuntimeError("redis no responde")


# ── La bandera de "Detener" ──────────────────────────────────────────────────

def test_cancelar_se_ve_desde_otra_lectura(cache_local):
    """El caso que estaba roto: quien pide la cancelacion y quien la lee son
    dos procesos distintos. Con la cache, el valor sale del mismo lugar."""
    from apps.energia.services.reporte import orquestador as orq

    assert orq._cache_leer("cancelar", FECHA) is None
    orq.cancelar_corrida(FECHA)
    assert orq._cache_leer("cancelar", FECHA) is True


def test_cancelar_es_por_fecha(cache_local):
    """Detener la corrida del 8 no puede detener la del 9."""
    from datetime import date

    from apps.energia.services.reporte import orquestador as orq

    orq.cancelar_corrida(FECHA)
    assert orq._cache_leer("cancelar", date(2026, 9, 9)) is None


def test_ultima_corrida_sin_nada_guardado_es_none(cache_local):
    from apps.energia.services.reporte import orquestador as orq

    assert orq.ultima_corrida(FECHA) is None


def test_resultado_de_la_corrida_se_lee_completo(cache_local):
    """Lo que el endpoint le pasa al frontend para avisar de fallidas."""
    from apps.energia.services.reporte import orquestador as orq

    orq._cache_escribir("ultima_corrida", FECHA, {
        "origen": "automatica", "fallidas": ["mgs0033"], "omitidas": [], "cancelado": False,
    }, 60)

    assert orq.ultima_corrida(FECHA) == {
        "origen": "automatica", "fallidas": ["mgs0033"], "omitidas": [], "cancelado": False,
    }


# ── Quien cede el paso ───────────────────────────────────────────────────────

def test_una_segunda_corrida_manual_no_arranca(cache_local):
    from apps.energia.services.reporte import orquestador as orq

    assert orq._tomar_en_curso(FECHA, "manual", exclusivo=True) is True
    assert orq._tomar_en_curso(FECHA, "manual", exclusivo=True) is False, (
        "dos corridas de la misma fecha escribirian las mismas filas en paralelo"
    )


def test_la_automatica_nunca_cede_el_paso(cache_local):
    """Tiene el horario: si a las 3:30 hay una manual en curso, arranca igual.
    Es el comportamiento de hoy, donde no hay marca de ninguna clase."""
    from apps.energia.services.reporte import orquestador as orq

    orq._tomar_en_curso(FECHA, "manual", exclusivo=True)
    assert orq._tomar_en_curso(FECHA, "automatica", exclusivo=False) is True
    assert orq.corrida_en_curso(FECHA)["origen"] == "automatica"


def test_al_liberar_la_marca_otra_corrida_puede_arrancar(cache_local):
    from apps.energia.services.reporte import orquestador as orq

    orq._tomar_en_curso(FECHA, "manual", exclusivo=True)
    orq._cache_borrar("en_curso", FECHA)
    assert orq._tomar_en_curso(FECHA, "manual", exclusivo=True) is True


# ── Con la cache caida, la corrida sigue ─────────────────────────────────────

def test_cache_caida_no_lanza_al_leer(monkeypatch):
    """Esta es la garantia que protege la corrida automatica. La bandera se lee
    entre frontera y frontera: una excepcion aca mataria el bucle a media lista
    por una caida de Redis que no tiene nada que ver con la clasificacion."""
    from apps.energia.services.reporte import orquestador as orq

    monkeypatch.setattr(orq, "cache", _CacheCaida())

    assert orq._cache_leer("cancelar", FECHA) is None
    assert orq.ultima_corrida(FECHA) is None
    assert orq.corrida_en_curso(FECHA) is None


def test_cache_caida_no_lanza_al_escribir_ni_al_borrar(monkeypatch):
    """El resultado de la corrida se escribe cuando ya termino y las filas ya
    estan en la base -- perder ese registro es un aviso menos en la pantalla,
    no una corrida perdida. Y el `finally` que libera la marca no puede tapar
    la excepcion real de la corrida con una suya."""
    from apps.energia.services.reporte import orquestador as orq

    monkeypatch.setattr(orq, "cache", _CacheCaida())

    orq._cache_escribir("ultima_corrida", FECHA, {"fallidas": []}, 60)
    orq.cancelar_corrida(FECHA)
    orq._cache_borrar("en_curso", FECHA)


def test_cache_caida_deja_arrancar_la_corrida(monkeypatch):
    """Falla hacia "seguir": sin cache no se sabe si hay otra corrida, y entre
    no clasificar el dia y arriesgar dos corridas simultaneas --lo que ya pasa
    hoy-- lo segundo es mucho menos grave."""
    from apps.energia.services.reporte import orquestador as orq

    monkeypatch.setattr(orq, "cache", _CacheCaida())

    assert orq._tomar_en_curso(FECHA, "manual", exclusivo=True) is True
    assert orq._tomar_en_curso(FECHA, "automatica", exclusivo=False) is True


# ── El contrato del endpoint ─────────────────────────────────────────────────

def test_estado_siempre_trae_fallidas_y_omitidas(cache_local):
    """El frontend hace `data.fallidas.length` sin preguntar. Sin las claves le
    daba un TypeError que se comia un catch silencioso -- justo el silencio que
    este endpoint existe para romper."""
    from rest_framework.request import Request
    from rest_framework.test import APIRequestFactory

    from api.v1.reporte_energia.views import ReporteEnergiaViewSet

    vista = ReporteEnergiaViewSet()
    # `Request` de DRF, no la de Django: _fecha() lee `query_params`.
    peticion = Request(APIRequestFactory().get(f"/?fecha={FECHA}"))
    respuesta = vista.ejecutar_estado(peticion)

    assert respuesta.data["fallidas"] == []
    assert respuesta.data["omitidas"] == []
