"""Cuánto del Reporte de Energía salió automático por CGM.

Es una pregunta distinta de la de los otros dos gráficos del resumen. Esos dicen
**de dónde salió el dato**; éste dice **cuánto salió solo**, que es la métrica
para saber si la automatización avanza.

Tres decisiones del cálculo, que son lo que se fija acá:

  · **Generación y consumo van juntos.** Lo que se mide es el reporte entero, no
    una de sus mitades. Un día-frontera de generación y uno de consumo cuentan
    igual: los dos son un reporte que salió solo o que no.
  · **Los días excluidos no entran en ningún lado.** No se reportaron a
    propósito, así que no son ni un éxito ni un fallo de la automatización.
    Contarlos como "otra fuente" empeoraría la métrica por una decisión
    deliberada -- el mismo criterio que ya usa `_distribucion_y_detalle`.
  · **Solo `cgm` cuenta como automático.** Es lo que se pidió: los que se
    reportan solos en Quoia.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _calcular(gen, con):
    from apps.energia.services.reporte.vistas import _distribucion_automatico

    return _distribucion_automatico(gen, con)


def _total(distribucion, etiqueta):
    return next((d["total"] for d in distribucion if d["etiqueta"] == etiqueta), 0)


AUTO = "Automático (CGM)"
OTRA = "Otra fuente"


def test_separa_cgm_del_resto():
    gen = [(1, "Frontera A", "cgm", 20), (1, "Frontera A", "principal", 10)]

    dist, _ = _calcular(gen, [])

    assert _total(dist, AUTO) == 20
    assert _total(dist, OTRA) == 10


def test_suma_generacion_y_consumo():
    """La métrica es sobre el reporte entero, no sobre una de sus mitades."""
    gen = [(1, "Frontera A", "cgm", 20)]
    con = [(1, "Frontera A", "cgm", 15)]

    dist, _ = _calcular(gen, con)

    assert _total(dist, AUTO) == 35


def test_los_excluidos_no_cuentan_en_ningun_lado():
    """Un día excluido no se reportó a propósito: no es un fallo de la
    automatización, y contarlo como "otra fuente" empeoraría la métrica por una
    decisión deliberada."""
    gen = [(1, "Frontera A", "cgm", 20), (1, "Frontera A", "excluida", 50)]

    dist, _ = _calcular(gen, [])

    assert _total(dist, AUTO) == 20
    assert _total(dist, OTRA) == 0


def test_no_distingue_mayusculas_ni_espacios():
    gen = [(1, "Frontera A", " CGM ", 5)]

    dist, _ = _calcular(gen, [])

    assert _total(dist, AUTO) == 5


def test_sin_fuente_cuenta_como_otra():
    """`None` es un día sin fuente: no salió solo."""
    gen = [(1, "Frontera A", None, 7)]

    dist, _ = _calcular(gen, [])

    assert _total(dist, OTRA) == 7


def test_una_categoria_vacia_no_aparece():
    """Sin días manuales no se dibuja una barra en cero."""
    gen = [(1, "Frontera A", "cgm", 10)]

    dist, _ = _calcular(gen, [])

    assert [d["etiqueta"] for d in dist] == [AUTO]


def test_el_automatico_va_primero():
    """Es el número que se viene a ver."""
    gen = [(1, "A", "cgm", 1), (2, "B", "principal", 1)]

    dist, _ = _calcular(gen, [])

    assert [d["etiqueta"] for d in dist] == [AUTO, OTRA]


def test_sin_datos_no_revienta():
    dist, detalle = _calcular([], [])

    assert dist == []
    assert detalle == []


# ── El detalle por frontera ─────────────────────────────────────────────────


def test_el_detalle_separa_cada_frontera():
    gen = [
        (1, "Frontera A", "cgm", 20),
        (1, "Frontera A", "principal", 10),
        (2, "Frontera B", "cgm", 30),
    ]

    _, detalle = _calcular(gen, [])

    a_auto = next(d for d in detalle if d["frontera_id"] == 1 and d["grupo"] == AUTO)
    assert a_auto["dias_grupo"] == 20
    assert a_auto["dias_totales"] == 30, "el total de la frontera son sus dos grupos"

    b = next(d for d in detalle if d["frontera_id"] == 2)
    assert b["dias_grupo"] == 30 and b["dias_totales"] == 30


def test_el_detalle_suma_los_dias_de_consumo_al_total():
    gen = [(1, "Frontera A", "cgm", 20)]
    con = [(1, "Frontera A", "medidor", 10)]

    _, detalle = _calcular(gen, con)

    assert {d["dias_totales"] for d in detalle} == {30}


def test_el_detalle_viene_ordenado_de_mayor_a_menor():
    gen = [(1, "Chica", "cgm", 2), (2, "Grande", "cgm", 40), (3, "Media", "cgm", 10)]

    _, detalle = _calcular(gen, [])

    assert [d["nombre_proyecto"] for d in detalle] == ["Grande", "Media", "Chica"]


def test_el_resumen_manda_las_dos_claves():
    """El contrato con el frontend."""
    import inspect

    from apps.energia.services.reporte import vistas

    fuente = inspect.getsource(vistas)

    assert '"distribucion_automatico": dist_auto,' in fuente
    assert '"detalle_automatico": detalle_auto,' in fuente


def test_el_front_conoce_las_dos_etiquetas():
    """El color sale de un diccionario indexado por la etiqueta EXACTA. Una
    etiqueta sin color cae al gris de "Otro", sin error: la barra aparece pero
    apagada y confundible."""
    from pathlib import Path

    vista = (
        Path(__file__).resolve().parents[2]
        / "unergy-operaciones-frontend"
        / "app/features/fronteras/components/ReporteEnergiaAutomatizacionView.vue"
    )
    if not vista.exists():
        pytest.skip("el repositorio del frontend no está al lado de este")

    fuente = vista.read_text(encoding="utf-8")

    assert "'Automático (CGM)':" in fuente
    assert "'Otra fuente':" in fuente


# ── Los días excluidos cuentan igual en los tres gráficos ───────────────────
# Hasta el 2026-09-15 no: `_distribucion_y_detalle` los sumaba a `dias_totales`
# y `_distribucion_automatico` no. Efecto visible: al abrir una frontera con
# días excluidos desde Generación, sus porcentajes no sumaban 100 --faltaba esa
# porción-- y desde Automáticos sí. El desglose divide `dias_grupo /
# dias_totales` y la barra de arriba divide sobre la suma de los grupos: con
# denominadores distintos, dos números que dicen lo mismo no coincidían.


def _filas_con_excluidos():
    return [(1, "Frontera", "cgm", 10), (1, "Frontera", "excluida", 5),
            (1, "Frontera", "principal", 10)]


def _detalles_de_los_dos_graficos():
    from apps.energia.services.reporte.vistas import (
        _GRUPO_FUENTE_GENERACION, _distribucion_automatico, _distribucion_y_detalle,
    )

    _, det_gen = _distribucion_y_detalle(_filas_con_excluidos(), _GRUPO_FUENTE_GENERACION)
    _, det_auto = _distribucion_automatico(_filas_con_excluidos(), [])
    return det_gen, det_auto


def test_los_dos_graficos_usan_el_mismo_denominador():
    det_gen, det_auto = _detalles_de_los_dos_graficos()

    assert {d["dias_totales"] for d in det_gen} == {20}
    assert {d["dias_totales"] for d in det_auto} == {20}


def test_los_porcentajes_del_desglose_suman_cien():
    """Era lo que se veía mal: 10 y 10 sobre 25 daban 40% + 40% = 80%."""
    for detalle in _detalles_de_los_dos_graficos():
        suma = sum(round(d["dias_grupo"] / d["dias_totales"] * 100) for d in detalle)
        assert suma == 100


def test_el_denominador_del_desglose_es_el_de_la_barra():
    from apps.energia.services.reporte.vistas import (
        _GRUPO_FUENTE_GENERACION, _distribucion_y_detalle,
    )

    dist, detalle = _distribucion_y_detalle(_filas_con_excluidos(), _GRUPO_FUENTE_GENERACION)

    assert sum(d["total"] for d in dist) == detalle[0]["dias_totales"]


def test_los_dias_excluidos_no_se_pierden():
    """Salen del porcentaje, pero quedan contados: no se reportaron a propósito,
    y eso es otra pregunta -- no una fuente peor."""
    for detalle in _detalles_de_los_dos_graficos():
        assert {d["dias_excluidos"] for d in detalle} == {5}
