"""La clave de Gemini se puede mandar desde el formulario, y no se filtra.

Quien lee los PDF de las facturas de XM es una IA, del lado de la API de
Liquidaciones. Esa API acepta una `api_key` propia; si no se le manda, usa la
suya —y cuando la de ellos falla, la subida se cae con un «0 successful, 1
failed» que desde acá no se puede arreglar.

Hasta ahora la clave solo podía venir de `LIQUIDACIONES_GEMINI_API_KEY` en el
`.env` del servidor, que se reescribe en cada deploy desde un secret de GitHub:
para ponerla hay que depender de quien tenga ese acceso. Ahora también se puede
escribir en el formulario de subida.

Eso mete un secreto en el camino navegador → servidor, así que hay tres reglas
que estas pruebas fijan:

  1. la del formulario MANDA sobre la del servidor —es la que la usuaria acaba
     de escribir a propósito—;
  2. sin ella se sigue usando la del servidor, que es el caso normal;
  3. **nunca vuelve en la respuesta**. Una clave que se devuelve termina en un
     log del navegador, en una captura de pantalla o en un reporte de error.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

CLAVE_FORM = "clave-que-escribio-la-usuaria"
CLAVE_SERVIDOR = "clave-del-servidor"


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _enviado(monkeypatch, *, api_key=None, en_servidor=""):
    """Devuelve el formulario que `subir_facturas_xm` le manda a la API."""
    from apps.liquidaciones.services import api_externa as api

    capturado = {}

    def _falso(method, path, **kwargs):
        capturado.update(kwargs.get("data") or {})
        return {"task_id": "t1", "invoice_ids": [1], "files_queued": 1}

    monkeypatch.setattr(api, "_request", _falso)
    monkeypatch.setattr(api.settings, "LIQUIDACIONES_GEMINI_API_KEY", en_servidor,
                        raising=False)

    api.subir_facturas_xm(
        [("f.pdf", b"%PDF-1.4", "application/pdf")], version="txf", api_key=api_key,
    )
    return capturado


def test_la_clave_del_formulario_se_manda(monkeypatch):
    assert _enviado(monkeypatch, api_key=CLAVE_FORM)["api_key"] == CLAVE_FORM


def test_la_del_formulario_manda_sobre_la_del_servidor(monkeypatch):
    """Quien la escribe en el momento sabe cuál quiere usar."""
    enviado = _enviado(monkeypatch, api_key=CLAVE_FORM, en_servidor=CLAVE_SERVIDOR)
    assert enviado["api_key"] == CLAVE_FORM


def test_sin_clave_en_el_formulario_se_usa_la_del_servidor(monkeypatch):
    """El caso normal: no hay que escribirla en cada subida."""
    assert _enviado(monkeypatch, en_servidor=CLAVE_SERVIDOR)["api_key"] == CLAVE_SERVIDOR


def test_sin_ninguna_no_se_manda_el_campo(monkeypatch):
    """Sin `api_key`, la API externa usa la suya. Mandar '' no es lo mismo."""
    assert "api_key" not in _enviado(monkeypatch)


def test_los_espacios_de_mas_no_cuentan_como_clave(monkeypatch):
    """Pegar una clave suele arrastrar un espacio o un salto de línea."""
    enviado = _enviado(monkeypatch, api_key=f"  {CLAVE_FORM}\n", en_servidor="")
    assert enviado["api_key"] == CLAVE_FORM


def test_una_clave_en_blanco_no_pisa_la_del_servidor(monkeypatch):
    """El campo vacío del formulario llega como '' y no debe borrar la del .env."""
    enviado = _enviado(monkeypatch, api_key="   ", en_servidor=CLAVE_SERVIDOR)
    assert enviado["api_key"] == CLAVE_SERVIDOR


def test_la_clave_nunca_vuelve_en_la_respuesta(monkeypatch):
    """Lo que se devuelve al navegador acaba en logs y capturas de pantalla."""
    from apps.liquidaciones.services import api_externa as api

    monkeypatch.setattr(api, "_request", lambda *a, **k: {
        "task_id": "t1", "invoice_ids": [1], "files_queued": 1,
        "api_key": CLAVE_FORM,          # si la API la devolviera, no se propaga
    })
    monkeypatch.setattr(api.settings, "LIQUIDACIONES_GEMINI_API_KEY", "", raising=False)

    salida = api.subir_facturas_xm(
        [("f.pdf", b"%PDF", "application/pdf")], version="txf", api_key=CLAVE_FORM,
    )
    assert CLAVE_FORM not in str(salida)
