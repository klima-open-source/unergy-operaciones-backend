"""Adoptar el reporte CGM a mano tiene que hacer que /enviar SE SALTE la fila.

Caso real: Paso Norte Consumo del 2026-09-07. Quoia habia reportado bien por su
cuenta (estado automatico), pero nuestra clasificacion desconfio del CGM --esa
frontera tiene historia de CGM doblado, ver el bloque de constantes de
clasificador_consumo-- y cayo a caso 'Historico' con Revisar Manualmente. Las
dos salidas eran malas:

  · validar la frontera tal cual  -> /enviar manda la matriz ENCIMA del reporte
    oficial de Quoia, porque `caso != 'CGM'`,
  · no validarla                  -> /enviar queda bloqueado para el dia
    completo (basta una fila pendiente, ver envio.enviar).

Lo que se agrego es 'cgm' como fuente manual. Y lo que este archivo vigila es la
parte que se puede equivocar en silencio: `_reporte_ya_valido()` mira campos
DISTINTOS segun el tipo -- `medidor_usado` en Generacion y `caso` en Consumo --
asi que `editar_curva` tiene que fijar LOS DOS. Si fijara solo uno, en Consumo
el envio no se saltaria la fila y le pisaria a Quoia su propio reporte, que es
justo el bug que se venia a arreglar.

`editar_curva` en si no tiene arnes: necesita base de datos y el repo no tiene
`pytest-django` (ver test_nombres_definidos.py). Lo que si se puede probar sin
base es la funcion que decide el salto, que es donde vive la consecuencia.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _rep(**kw):
    from types import SimpleNamespace

    base = dict(caso="Histórico", medidor_usado="historico")
    base.update(kw)
    return SimpleNamespace(**base)


# ── Lo que deja editar_curva al adoptar CGM ──────────────────────────────────

def test_consumo_con_cgm_no_se_envia():
    """Lo que fija editar_curva en Consumo: caso 'CGM' + medidor_usado 'cgm'."""
    from apps.energia.services.reporte.envio import _reporte_ya_valido

    rep = _rep(caso="CGM", medidor_usado="cgm")
    assert _reporte_ya_valido(rep, es_generacion=False) is True


def test_generacion_con_cgm_no_se_envia():
    """Y en Generacion: caso 1 + medidor_usado 'cgm'."""
    from apps.energia.services.reporte.envio import _reporte_ya_valido

    rep = _rep(caso=1, medidor_usado="cgm")
    assert _reporte_ya_valido(rep, es_generacion=True) is True


# ── Por que hay que fijar los DOS campos y no uno ────────────────────────────

def test_en_consumo_medidor_usado_solo_no_alcanza():
    """El error que se cometeria fijando solo `medidor_usado`: en Consumo el
    chequeo mira `caso`, asi que la fila se enviaria igual y le pisaria a Quoia
    su reporte oficial con nuestra estimacion."""
    from apps.energia.services.reporte.envio import _reporte_ya_valido

    rep = _rep(caso="Histórico", medidor_usado="cgm")
    assert _reporte_ya_valido(rep, es_generacion=False) is False


def test_en_generacion_caso_solo_no_alcanza():
    """El simetrico: en Generacion el chequeo mira `medidor_usado`."""
    from apps.energia.services.reporte.envio import _reporte_ya_valido

    rep = _rep(caso=1, medidor_usado="historico")
    assert _reporte_ya_valido(rep, es_generacion=True) is False


# ── El caso de Paso Norte, tal como estaba ───────────────────────────────────

def test_paso_norte_como_estaba_si_se_enviaba():
    """La fila del 7/09 antes del arreglo: validarla mandaba la matriz."""
    from apps.energia.services.reporte.envio import _reporte_ya_valido

    assert _reporte_ya_valido(_rep(), es_generacion=False) is False


# ── Lo que no se toco ────────────────────────────────────────────────────────

def test_una_frontera_excluida_sigue_sin_enviarse():
    from apps.energia.services.reporte.envio import _reporte_ya_valido

    rep = _rep(medidor_usado="excluida")
    assert _reporte_ya_valido(rep, es_generacion=True) is True
    assert _reporte_ya_valido(rep, es_generacion=False) is True


def test_cgm_es_una_fuente_manual_declarada():
    """Si no estuviera en la lista, `fuente: 'cgm'` caeria en el generico
    'editado_manualmente' y no fijaria ningun `caso`."""
    from api.v1.reporte_energia.serializers import FUENTES_MANUALES

    assert "cgm" in FUENTES_MANUALES
