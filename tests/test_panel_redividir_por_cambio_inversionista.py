"""Cambiar los inversionistas de un proyecto redivide sus paneles solo.

Las líneas de un panel son un SNAPSHOT: se dividen entre los inversionistas al
momento de armarlo. Si después cambia quién invierte —GD Biosolar pasó de
INVERSIONES BIOSOSTENIBLES a Inversiones Manrique Martheyn el 2026-07-01— los
paneles ya armados siguen mostrando al inversionista viejo hasta que alguien se
acuerde de entrar a redividir.

Acordarse no es un mecanismo. El reparto queda mal justo en el mes del cambio,
que es el que se va a facturar, y nada lo avisa.

`redividir` ya sabía hacerlo bien: compara por ID de inversionista, no solo por
porcentaje, así que detecta un cambio de cliente aunque el porcentaje siga en
100 %. Lo que faltaba era dispararlo. Ahora las tres escrituras de inversionistas
—agregar, editar y eliminar— redividen los paneles del proyecto.

El redividido NO puede tumbar el cambio de inversionista: ese es lo que la
usuaria pidió, y el reparto es su consecuencia. Si falla, se registra y el
cambio se conserva.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def test_redivide_cada_periodo_y_tipo_del_proyecto(monkeypatch):
    """Un proyecto tiene varios paneles: mayo, junio y julio, oficial y preliq."""
    from apps.contabilidad.services import panel as svc

    monkeypatch.setattr(
        svc, "_periodos_y_tipos_de", lambda proyecto_id: [
            ("2026-06", "oficial"), ("2026-07", "oficial"), ("2026-07", "preliquidacion"),
        ],
    )
    llamadas = []
    monkeypatch.setattr(svc, "redividir", lambda **kw: llamadas.append(kw) or {
        "n_redivididos": 1, "n_saltados": 0,
    })

    resumen = svc.redividir_proyecto(6)

    assert [(c["periodo"], c["tipo"]) for c in llamadas] == [
        ("2026-06", "oficial"), ("2026-07", "oficial"), ("2026-07", "preliquidacion"),
    ]
    assert all(c["proyecto_id"] == 6 for c in llamadas)
    assert resumen["n_redivididos"] == 3


def test_sin_paneles_no_hace_nada(monkeypatch):
    from apps.contabilidad.services import panel as svc

    monkeypatch.setattr(svc, "_periodos_y_tipos_de", lambda proyecto_id: [])
    monkeypatch.setattr(svc, "redividir", lambda **kw: pytest.fail("no debió llamarse"))

    assert svc.redividir_proyecto(6) == {"n_redivididos": 0, "n_saltados": 0, "paneles": []}


def test_si_el_redividido_falla_no_se_pierde_el_cambio(monkeypatch):
    """El cambio de inversionista es lo que pidió la usuaria; el reparto es su
    consecuencia. Un fallo aquí se registra, no se propaga."""
    from apps.contabilidad.services import panel as svc

    monkeypatch.setattr(svc, "_periodos_y_tipos_de", lambda proyecto_id: [("2026-07", "oficial")])

    def _explota(**kw):
        raise RuntimeError("la base se cayó")

    monkeypatch.setattr(svc, "redividir", _explota)

    resumen = svc.redividir_proyecto(6)          # no levanta
    assert resumen["n_redivididos"] == 0
    assert "error" in resumen
