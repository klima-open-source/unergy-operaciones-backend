"""El resumen separa CGM y lo reportado por terceros de "Medidor".

El gráfico "Fuente usada" del Reporte de Energía responde una pregunta: **¿de
dónde salió el dato que reportamos?** Y "Medidor" venía juntando diez fuentes
distintas, dos de las cuales no son un medidor nuestro:

  · `cgm` -- el dato oficial del mercado. Agrupado con los medidores, no se
    podía ver cuánto del reporte se sostiene en el CGM y cuánto en lo que
    leemos nosotros.
  · `excel_terceros` y `externo` -- un Excel que manda un tercero y un dato que
    reporta otra empresa. Ninguna de las dos la medimos, y ninguna pasa por el
    mercado.

El caso que lo destapó (2026-09-14): **Complejo Industrial Cedillanos**
aparecía con "Medidor 100%" y su desglose era 27 días de Excel de terceros y 3
de otra empresa. Ni una sola lectura propia, contada como si la hubiéramos
medido nosotros.

Lo que se fija acá es el mapeo, que es una tabla fácil de tocar sin querer, y
sobre todo los tres que NO se movieron: el reconectador es equipo nuestro y se
queda en Medidor, y las variantes `principal_*`/`respaldo_*` también.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _grupo_gen(fuente):
    from apps.energia.services.reporte.vistas import _GRUPO_FUENTE_GENERACION

    return _GRUPO_FUENTE_GENERACION[fuente]


def _etiqueta(grupo):
    from apps.energia.services.reporte.vistas import _ETIQUETA_GRUPO_FUENTE

    return _ETIQUETA_GRUPO_FUENTE[grupo]


# ── Lo que se separó ────────────────────────────────────────────────────────


def test_el_cgm_ya_no_cuenta_como_medidor():
    assert _grupo_gen("cgm") == "cgm"
    assert _etiqueta("cgm") == "CGM"


def test_el_excel_de_terceros_ya_no_cuenta_como_medidor():
    """El caso Cedillanos: 27 días de Excel contados como medidor propio."""
    assert _grupo_gen("excel_terceros") == "terceros"


def test_lo_que_reporta_otra_empresa_tampoco():
    assert _grupo_gen("externo") == "terceros"


def test_los_dos_de_terceros_caen_en_la_misma_barra():
    assert _grupo_gen("excel_terceros") == _grupo_gen("externo")
    assert _etiqueta("terceros") == "Reportado por terceros"


# ── Lo que NO se movió ──────────────────────────────────────────────────────


@pytest.mark.parametrize("fuente", [
    "principal", "respaldo",
    "principal_sin_cgm", "respaldo_sin_cgm",
    "principal_sin_historico", "respaldo_sin_historico",
])
def test_los_medidores_propios_siguen_en_medidor(fuente):
    assert _grupo_gen(fuente) == "medidor"


def test_el_reconectador_sigue_en_medidor():
    """Es equipo NUESTRO: la lectura es propia aunque el aparato sea otro."""
    assert _grupo_gen("reconectador") == "medidor"


@pytest.mark.parametrize("fuente", ["inversores", "solenium_power"])
def test_los_inversores_siguen_donde_estaban(fuente):
    assert _grupo_gen(fuente) == "inversor"


@pytest.mark.parametrize("fuente", [
    "crudos", "crudos_parcial", "historico", "historico_vecino",
    "editado_manualmente", "relleno_horario",
])
def test_las_estimaciones_siguen_donde_estaban(fuente):
    assert _grupo_gen(fuente) == "estimacion"


def test_apagado_sigue_siendo_categoria_propia():
    """Es un estado CONFIRMADO, no una estimación de un dato faltante."""
    assert _grupo_gen("ninguno") == "apagado"


# ── El consumo ──────────────────────────────────────────────────────────────


def test_en_consumo_el_cgm_tambien_se_separa():
    from apps.energia.services.reporte.vistas import _GRUPO_FUENTE_CONSUMO

    assert _GRUPO_FUENTE_CONSUMO["cgm"] == "cgm"
    assert _GRUPO_FUENTE_CONSUMO["medidor"] == "medidor"


# ── El orden de las barras ──────────────────────────────────────────────────


def test_las_barras_van_de_mas_a_menos_respaldo():
    """El orden no es estético: el gráfico se lee de izquierda a derecha y la
    posición dice cuánto se sostiene el dato."""
    from apps.energia.services.reporte.vistas import _ORDEN_GRUPO_FUENTE

    assert _ORDEN_GRUPO_FUENTE == [
        "cgm", "medidor", "inversor", "terceros", "estimacion", "apagado",
        "sin_fuente", "otro",
    ]


def test_todo_grupo_del_mapa_tiene_etiqueta_y_puesto():
    """Un grupo sin etiqueta revienta con KeyError al armar el resumen, y uno
    sin puesto en el orden desaparece del gráfico sin fallar -- que es peor."""
    from apps.energia.services.reporte.vistas import (
        _ETIQUETA_GRUPO_FUENTE,
        _GRUPO_FUENTE_CONSUMO,
        _GRUPO_FUENTE_GENERACION,
        _ORDEN_GRUPO_FUENTE,
    )

    usados = set(_GRUPO_FUENTE_GENERACION.values()) | set(_GRUPO_FUENTE_CONSUMO.values())
    usados.add("otro")  # el default de `mapa.get(low, "otro")`

    assert usados <= set(_ETIQUETA_GRUPO_FUENTE), "falta una etiqueta"
    assert usados <= set(_ORDEN_GRUPO_FUENTE), "falta un puesto en el orden"


def test_el_front_conoce_las_dos_etiquetas_nuevas():
    """El color sale de un diccionario del frontend indexado por la etiqueta
    EXACTA que manda el backend. Una etiqueta sin color cae al gris de "Otro",
    sin error: la barra aparece, pero apagada y confundible."""
    from pathlib import Path

    vista = (
        Path(__file__).resolve().parents[2]
        / "unergy-operaciones-frontend"
        / "app/features/fronteras/components/ReporteEnergiaAutomatizacionView.vue"
    )
    if not vista.exists():
        pytest.skip("el repositorio del frontend no está al lado de este")

    fuente = vista.read_text(encoding="utf-8")

    assert "'CGM':" in fuente
    assert "'Reportado por terceros':" in fuente
