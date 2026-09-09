"""La curva del CGM se guarda al clasificar; el panel no vuelve a pedirla.

`curva_cgm_referencia` es la cuarta curva de referencia de la fila, junto a las
de medidor principal/respaldo, Solenium y reconectador, y por la misma razon:
se consulta UNA vez en la corrida de madrugada (donde 2 segundos por frontera
no le importan a nadie) y el panel la lee de la base, que se abre muchas veces
al dia. Este mismo archivo existe porque en ese archivo hay un precedente
explicito -- Solenium dejo de consultarse en vivo en `_construir_detalle`
justamente porque costaba ~2s por apertura.

Lo que se prueba aca es la GUARDA, `_pedir_cgm_en_vivo()`, que es lo que puede
regresar en silencio: si alguien la quita, el panel vuelve a pagar una llamada
de red en cada apertura de cada frontera; si alguien invierte una condicion, la
opcion de adoptar el CGM deja de aparecer y nadie se entera hasta que la
necesita.

Sin base de datos: la funcion solo lee tres atributos de la fila.
"""
from types import SimpleNamespace

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _fila(**kw):
    """Una fila como la de Paso Norte Consumo 2026-09-07: reporte automatico
    valido en Quoia, pero la clasificacion se fue por el historico."""
    base = dict(
        curva_cgm_referencia=None,
        estado_reporte="OK",
        medidor_usado="historico",
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── Cuando SI hay que preguntarle a Quoia ────────────────────────────────────

def test_fila_vieja_con_reporte_valido_y_otra_fuente_si_pide():
    """El unico caso que justifica la llamada: no hay curva guardada (fila
    anterior a la columna), Quoia si reporto, y reportamos otra cosa."""
    from apps.energia.services.reporte.vistas import _pedir_cgm_en_vivo

    assert _pedir_cgm_en_vivo(_fila()) is True


def test_warning_tambien_cuenta_como_reporte_valido():
    """Mismos estados que ESTADOS_AUTOMATICO en los clasificadores."""
    from apps.energia.services.reporte.vistas import _pedir_cgm_en_vivo

    assert _pedir_cgm_en_vivo(_fila(estado_reporte="WARNING")) is True


# ── Cuando NO, que es casi siempre ───────────────────────────────────────────

def test_con_la_curva_ya_guardada_no_pide():
    """El caso normal a partir de la primera corrida con la columna: se lee de
    la base y no se toca la red."""
    from apps.energia.services.reporte.vistas import _pedir_cgm_en_vivo

    assert _pedir_cgm_en_vivo(_fila(curva_cgm_referencia=[1.0] * 24)) is False


def test_si_el_cgm_ya_gano_no_pide():
    """Sus horas SON curva_final, y el desplegable ni aparece (caso confiado)."""
    from apps.energia.services.reporte.vistas import _pedir_cgm_en_vivo

    assert _pedir_cgm_en_vivo(_fila(medidor_usado="cgm", estado_reporte="OK")) is False


@pytest.mark.parametrize("estado", [None, "", "ERROR", "PENDING", "FAILED"])
def test_sin_reporte_automatico_valido_no_pide(estado):
    """No hubo reporte que adoptar: la opcion sale deshabilitada igual, asi que
    la llamada seria a cambio de nada."""
    from apps.energia.services.reporte.vistas import _pedir_cgm_en_vivo

    assert _pedir_cgm_en_vivo(_fila(estado_reporte=estado)) is False


def test_una_curva_guardada_manda_incluso_sin_estado_reporte():
    """El orden de los chequeos importa: si ya esta guardada no se pregunta
    nada mas, ni siquiera por el estado."""
    from apps.energia.services.reporte.vistas import _pedir_cgm_en_vivo

    fila = _fila(curva_cgm_referencia=[1.0] * 24, estado_reporte=None)
    assert _pedir_cgm_en_vivo(fila) is False


# ── La columna existe en las dos tablas ──────────────────────────────────────

@pytest.mark.parametrize("modelo", ["ReporteEnergiaGeneracion", "ReporteEnergiaConsumo"])
def test_las_dos_tablas_tienen_la_columna(modelo):
    """Generacion y Consumo -- el reporte del CGM aplica a las dos."""
    from apps.energia import models

    campos = {f.name for f in getattr(models, modelo)._meta.get_fields()}
    assert "curva_cgm_referencia" in campos
