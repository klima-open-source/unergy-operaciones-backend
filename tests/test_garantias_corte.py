"""en_vivo(corte) replica un corte pasado: enhebra la fecha al precio (7d hasta el
corte) y al balance (generación hasta el corte). Deps externas mockeadas.

Sigue el patrón del repo (sin pytest-django): django.setup() en un fixture y el
módulo bajo prueba se importa DESPUÉS, dentro del test.
"""
from datetime import date

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _balance_vacio():
    celda = {"real": 0.0, "proyectado": 0.0, "total": 0.0, "n_plantas": 0}
    return {"balance": {
        "ungg": {k: dict(celda) for k in (
            "venta_bolsa", "compra_bolsa_directa", "compra_bolsa_no_directa",
            "compra_bolsa_total", "neto")},
        "ungc": {"venta_bolsa": dict(celda)},
    }}


def test_en_vivo_enhebra_el_corte_al_precio_y_al_balance(monkeypatch):
    from apps.garantias.services import proyecciones as svc

    corte = date(2026, 9, 12)
    visto = {}

    def fake_balance(anio, mes, corte=None):
        visto.setdefault("balance_cortes", []).append(corte)
        return _balance_vacio()

    def fake_precio(hasta=None):
        visto["precio_hasta"] = hasta
        return 500.0

    monkeypatch.setattr(svc, "_balance", fake_balance)
    monkeypatch.setattr(svc, "_precio_bolsa", fake_precio)
    monkeypatch.setattr(svc, "neto_de_ventana", lambda a, m, d: None)
    monkeypatch.setattr(svc, "pagado_por_periodo", lambda: {})
    # `_regulatorio` va a buscar el archivo del mes a Google Drive. Sin este
    # parche la prueba solo pasa en una máquina con `GOOGLE_SERVICE_ACCOUNT_JSON`
    # configurado -- en CI no lo hay, y ahí fallaba. Lo que se prueba acá es que
    # el corte se enhebre al precio y al balance; el costo regulatorio no
    # participa, así que basta con que devuelva su forma.
    monkeypatch.setattr(
        svc, "_regulatorio",
        lambda anio, mes: {"valor": 0.0, "anio": anio, "mes": mes, "fallback": False},
    )

    r = svc.en_vivo(hoy=corte)

    assert r["fecha_corte"] == "2026-09-12"
    # ventanas: resto de septiembre + octubre completo
    assert {(v["anio"], v["mes"]) for v in r["ventanas"]} == {(2026, 9), (2026, 10)}
    # el corte se enhebró a ambas dependencias
    assert visto["precio_hasta"] == corte
    assert visto["balance_cortes"] and all(c == corte for c in visto["balance_cortes"])
