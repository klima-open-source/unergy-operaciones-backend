"""El API comprime sus respuestas.

`GET /api/v1/fallas?page=1&size=500` devolvía **Cloudflare 1102 — Worker
exceeded resource limits**. No era un error del backend: el origen respondía
bien, pero mandaba varios MB de JSON *sin comprimir* y el Worker que sirve
operaciones.unergy.io se quedaba sin recursos manejando el cuerpo. No hay
nginx/caddy delante de gunicorn que comprima por nosotros — `docker-compose.yml`
publica gunicorn directo — así que la compresión tiene que salir de Django.

Lo que fija esta prueba es el `GZipMiddleware` de `config/settings.py`: es una
sola línea, invisible en cualquier revisión de código de negocio, y sacarla
reintroduce el 1102 sin que falle nada más del suite.

Se usa `/api/v1/mapa/operadores` porque es el único endpoint sin autenticación
(`AllowAny`) que no toca la base `operations`: así la prueba ejercita la cadena
real (WSGI -> MIDDLEWARE -> vista) sin montar una base de test. El payload se
monkeypatchea grande a propósito: Django no comprime respuestas de menos de 200
bytes.
"""

import gzip
import json
import os

import pytest

pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

RUTA = "/api/v1/mapa/operadores"


@pytest.fixture
def cliente(monkeypatch):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    import django

    django.setup()
    from django.test import Client

    from apps.proyectos.services import mapa_externo

    # 40 operadores: pasa de sobra los 200 bytes bajo los que Django no
    # comprime, y siendo repetitivo el gzip queda mucho mas chico (si no,
    # el middleware descarta la version comprimida por no ayudar).
    payload = [{"code": f"OR{i:03d}", "name": f"Operador de Red {i}"} for i in range(40)]
    monkeypatch.setattr(mapa_externo, "operadores", lambda: payload)
    return Client(), payload


def test_con_accept_encoding_gzip_la_respuesta_viene_comprimida(cliente):
    client, payload = cliente
    respuesta = client.get(RUTA, HTTP_ACCEPT_ENCODING="gzip")

    assert respuesta.status_code == 200
    assert len(json.dumps(payload)) > 200, "el payload no llega al umbral de Django"
    assert respuesta.headers.get("Content-Encoding") == "gzip", (
        "la respuesta salió sin comprimir: se perdió "
        "`django.middleware.gzip.GZipMiddleware` de MIDDLEWARE y "
        "`/api/v1/fallas?size=500` vuelve a matar al Worker de Cloudflare con un 1102"
    )
    assert json.loads(gzip.decompress(respuesta.content)) == payload
    assert len(respuesta.content) < len(json.dumps(payload))


def test_sin_accept_encoding_la_respuesta_viene_en_claro(cliente):
    client, payload = cliente
    respuesta = client.get(RUTA)

    assert respuesta.status_code == 200
    assert "Content-Encoding" not in respuesta.headers
    assert json.loads(respuesta.content) == payload


# ── El tope de filas ─────────────────────────────────────────────────────────
#
# gzip bajo el cuerpo pero el 1102 seguia: el tope de 500 filas (5000 en fallas)
# era lo que dejaba pedir un cuerpo que el Worker no aguanta. Ahora son 100 en
# TODA la API, y pedir mas RECORTA en vez de dar 422 -- un 422 dejaria al
# frontend, que hoy pide `size=500`, sin listado.

def test_el_tope_de_filas_es_100_y_recorta_en_vez_de_fallar():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    import django

    django.setup()
    from rest_framework.test import APIRequestFactory

    from api.pagination import TOPE_FILAS, BasePagination, recortar
    from api.v1.fallas.views import PaginacionFallas

    assert TOPE_FILAS == 100
    # Las tres clases de paginacion de la API: fallas heredaba un 5000 propio.
    for clase in (BasePagination, PaginacionFallas):
        assert clase.max_page_size == 100, clase.__name__

    assert recortar(500) == 100 and recortar(20) == 20

    # DRF recorta, no lanza: es lo que hace que `?size=500` siga siendo un 200.
    peticion = APIRequestFactory().get("/api/v1/fallas?size=500")
    from rest_framework.request import Request

    assert BasePagination().get_page_size(Request(peticion)) == 100
