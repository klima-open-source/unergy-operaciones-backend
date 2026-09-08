"""Las líneas de un panel se leen con `.lineas.all()`, nunca `.lineas` a secas.

Bug real (2026-09-08): el Panel Contable y **todo** el espejo de Liquidaciones
devolvían 500 desde el despliegue de la migración a Django:

    TypeError: 'RelatedManager' object is not iterable

En SQLAlchemy `panel.lineas` era una lista y se podía ordenar directo. En Django
es un `RelatedManager`, y hay que pedirle la consulta con `.all()`. El port dejó
`sorted(panel.lineas, ...)` intacto en dos sitios:

    apps/liquidaciones/services/resumen_panel.py   -> GET /liquidaciones/resumen-panel
    apps/contabilidad/services/panel.py            -> GET /panel-contable

Se llevaba por delante la vista Panel Contable entera y los tres tabs de
Liquidaciones, más el detalle, el PDF y las gráficas de tendencia.

Las 2 720 pruebas pasaban con eso roto porque las dos funciones se prueban como
funciones puras y el `_mk_panel` de `tests/test_liquidaciones.py` les pasa
`lineas=` como una **lista** de `SimpleNamespace`: el tipo real nunca entraba al
test.

Este test cierra ese hueco **sin base de datos**: a un modelo sin guardar (pero
con `pk`) se le puede pedir el manager inverso sin consultar nada, y `sorted()`
sobre él revienta con `TypeError` antes de tocar la base. Después del arreglo,
`.all()` sí intenta consultar, así que aquí solo se exige que **no** sea ese
`TypeError`: cualquier otro fallo depende del entorno (sin Postgres delante,
Django levanta un error de conexión) y no es lo que este test vigila.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _panel_sin_guardar():
    """Un `PanelContable` con `pk` pero sin fila: da un manager, no consulta."""
    from apps.contabilidad.models import PanelContable

    return PanelContable(id=1, proyecto_id=1, periodo="2026-07", tipo="preliquidacion")


def _sin_relatedmanager(fn, *args, **kwargs):
    """Ejecuta `fn` y falla solo si trata un manager inverso como iterable."""
    try:
        fn(*args, **kwargs)
    except TypeError as exc:
        if "RelatedManager" in str(exc):
            pytest.fail(
                f"{fn.__module__}.{fn.__name__} itera una relación inversa sin "
                f"`.all()`: {exc}"
            )
        raise
    except Exception:
        # Sin base de datos delante, `.all()` falla al conectarse. Ese fallo es
        # del entorno, no del bug que este test vigila.
        pass


def test_resumen_panel_no_itera_el_manager():
    """`GET /liquidaciones/resumen-panel` — el espejo de Liquidaciones."""
    from apps.liquidaciones.services import resumen_panel

    _sin_relatedmanager(
        resumen_panel.construir,
        [_panel_sin_guardar()], "2026-07", "preliquidacion", {}, {}, {}, {},
    )


def test_serializar_panel_no_itera_el_manager():
    """`GET /panel-contable` — el listado del Panel Contable."""
    from apps.contabilidad.services import panel

    _sin_relatedmanager(panel._serializar_panel, _panel_sin_guardar(), {})
