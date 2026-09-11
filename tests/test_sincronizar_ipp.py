"""Traer el IPP del DANE desde la API de Liquidaciones a nuestro `ipp_mensual`.

El IPP se consultaba en dos sitios sin hablarse: la API de Liquidaciones lo pide
al DANE para liquidar, y Facturación lo lee de nuestra tabla `ipp_mensual`, que
se llenaba a mano. **Es el mismo número** — los 8 meses que coincidían el
2026-09-10 eran idénticos hasta el último decimal.

El costo de tenerlos separados era real: la API tenía **15 meses** que nosotros
no, entre ellos 2026-08 (el que se iba a facturar) y todo oct-2024 a nov-2025,
que es de donde salen los `ipp_base` de los PPA. Sin esos meses, esas líneas
caen en `sin_ipp_base` y no se pueden calcular.

Dos trampas del formato de la API:

  * **Trae más de una fila por mes** (39 filas para 23 meses): guarda un registro
    por cada consulta al DANE, fechado el día en que se consultó, no el día 1.
    Hay que quedarse con el de `date` más reciente.
  * Esos duplicados traían el mismo valor en los 23 meses revisados, pero el
    código no puede depender de esa suerte.

Se dispara a mano, desde Liquidaciones: el IPP se publica una vez al mes y
Jessica prefiere decidir cuándo, no que corra solo.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _elegir():
    from apps.ppa.services.ipp import elegir_por_mes

    return elegir_por_mes


def test_se_queda_con_la_consulta_mas_reciente_de_cada_mes():
    """La API guarda una fila por consulta al DANE, no una por mes."""
    filas = [
        {"year": 2026, "month": 3, "ipp": 187.00, "date": "2026-03-10T00:00:00-05:00"},
        {"year": 2026, "month": 3, "ipp": 187.17, "date": "2026-03-15T00:00:00-05:00"},
        {"year": 2026, "month": 4, "ipp": 188.67, "date": "2026-04-11T00:00:00-05:00"},
    ]
    assert _elegir()(filas) == {(2026, 3): 187.17, (2026, 4): 188.67}


def test_ignora_las_filas_inservibles():
    """Sin año, sin mes o sin valor no hay nada que guardar."""
    filas = [
        {"year": 2026, "month": 5, "ipp": None, "date": "2026-05-01"},
        {"year": None, "month": 6, "ipp": 1.0, "date": "2026-06-01"},
        {"year": 2026, "month": None, "ipp": 1.0, "date": "2026-06-01"},
        {"year": 2026, "month": 7, "ipp": 186.35, "date": "2026-07-01"},
    ]
    assert _elegir()(filas) == {(2026, 7): 186.35}


def test_una_fila_sin_fecha_no_tumba_el_mes():
    """`date` puede faltar; se ordena igual y el mes no se pierde."""
    filas = [
        {"year": 2026, "month": 8, "ipp": 185.33, "date": None},
        {"year": 2026, "month": 8, "ipp": 185.40, "date": "2026-08-20"},
    ]
    assert _elegir()(filas) == {(2026, 8): 185.40}


# ── Qué hacer con cada mes ───────────────────────────────────────────────────
#
# La decisión se separa del guardado para poder probarla: el repo no tiene
# `pytest-django`, así que una prueba con base de datos exigiría una dependencia
# nueva. Lo que importa —qué se crea, qué se pisa y qué se deja— es puro.

def _clasificar():
    from apps.ppa.services.ipp import clasificar

    return clasificar


def test_crea_los_que_faltan_y_deja_en_paz_los_iguales():
    r = _clasificar()(
        de_la_api={(2026, 7): 186.35, (2026, 8): 185.33},
        nuestros={(2026, 7): 186.35},
    )
    assert r["crear"] == {(2026, 8): 185.33}
    assert r["actualizar"] == {}
    assert r["sin_cambio"] == 1


def test_un_valor_distinto_se_actualiza_y_se_reporta():
    """La API es la misma fuente que usa la liquidación: manda ella. Pero el
    cambio se informa, porque mueve lo que ya se facturó."""
    r = _clasificar()(
        de_la_api={(2026, 7): 186.35},
        nuestros={(2026, 7): 180.0},
    )
    assert r["actualizar"] == {(2026, 7): 186.35}
    assert r["cambios"] == [{"periodo": "2026-07", "antes": 180.0, "ahora": 186.35}]


def test_una_diferencia_de_redondeo_no_cuenta_como_cambio():
    """`valor` es DecimalField(4 decimales); comparar en float da falsos cambios."""
    r = _clasificar()(de_la_api={(2026, 7): 186.35}, nuestros={(2026, 7): 186.35000001})
    assert r["actualizar"] == {} and r["sin_cambio"] == 1


def test_lo_nuestro_que_la_api_no_tiene_se_conserva():
    """Sincronizar TRAE; nunca borra un mes que alguien cargó a mano."""
    r = _clasificar()(de_la_api={(2026, 7): 186.35}, nuestros={(2020, 1): 100.0, (2026, 7): 186.35})
    assert r["crear"] == {} and r["actualizar"] == {}
    assert "borrar" not in r
