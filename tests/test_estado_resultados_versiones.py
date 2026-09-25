"""El Excel del estado de resultados NO se pide con `version`.

`GET /api/liquidaciones/get_income_statement/` acepta, según su propio Swagger:

    last_version   "Version of the invoices to get data FROM"
    new_version    "Version of the invoices to get data TO"
    project        opcional, un solo proyecto
    month, year    obligatorios

Nosotros le mandábamos `version`, que ese endpoint **no conoce**. La API lo
ignoraba y generaba con sus valores por defecto, así que elegir `tx3` en la
plataforma no cambiaba nada: siempre salía lo mismo. Reportado por Jessica el
2026-09-25, que lo corría a mano desde el Swagger justamente porque la vista no
tenía esos campos.

Ojo con la trampa: el endpoint del ER en **JSON** (`income_statement_data`) sí
usa `version`, y ese está bien. Son dos endpoints distintos con dos contratos
distintos, y la guía de integración describe solo el segundo — por eso el error
pasó desapercibido.

Estas pruebas fijan los nombres de los parámetros. Son los únicos que la API
mira: un typo acá no da error, da un archivo silenciosamente equivocado.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _params(monkeypatch, **kwargs):
    """Los `params` con que se llama a la API, sin salir a la red."""
    from apps.liquidaciones.services import api_externa as api

    capturado = {}

    def _falso(method, path, **kw):
        capturado["path"] = path
        capturado.update(kw.get("params") or {})
        return {"task_id": "t1"}

    monkeypatch.setattr(api, "_request", _falso)
    api.generar_estado_resultados(**kwargs)
    return capturado


BASE = {"month": 6, "year": 2025}


def test_manda_las_dos_versiones_no_una(monkeypatch):
    p = _params(monkeypatch, **BASE, last_version="txf", new_version="tx3")
    assert p["last_version"] == "txf"
    assert p["new_version"] == "tx3"


def test_no_manda_el_parametro_version(monkeypatch):
    """`version` no existe en este endpoint: mandarlo es ruido que la API ignora
    mientras cree que no le pidieron nada."""
    p = _params(monkeypatch, **BASE, last_version="txf", new_version="tx3")
    assert "version" not in p


def test_el_mes_y_el_año_siguen_yendo(monkeypatch):
    p = _params(monkeypatch, **BASE, last_version="txf", new_version="txf")
    assert p["month"] == 6 and p["year"] == 2025


def test_el_proyecto_es_opcional_y_se_omite_si_no_viene(monkeypatch):
    """Sin `project` la API genera todos los proyectos del período."""
    p = _params(monkeypatch, **BASE, last_version="txf", new_version="txf")
    assert "project" not in p


def test_con_proyecto_se_manda_solo_ese(monkeypatch):
    p = _params(monkeypatch, **BASE, last_version="txf", new_version="tx3",
                project="bayunca")
    assert p["project"] == "bayunca"


def test_una_version_vacia_no_se_manda(monkeypatch):
    """Mandar `last_version=''` no es lo mismo que no mandarlo: la API lo
    tomaría como una versión inválida."""
    p = _params(monkeypatch, **BASE, last_version=None, new_version="tx3")
    assert "last_version" not in p
    assert p["new_version"] == "tx3"
