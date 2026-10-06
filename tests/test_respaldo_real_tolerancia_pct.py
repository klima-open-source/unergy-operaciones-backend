"""El respaldo real del medidor se reporta si su total diario está a 1.5% o
menos del principal, arriba o abajo -- igual en Generación y en Consumo.

Antes era un margen fijo de 1.5 kWh: en Generación, con totales de cientos o
miles de kWh, el respaldo salía casi siempre "estimado" aunque el medidor
estuviera a menos del 1% (reportado por Sara 2026-09-30). El 2026-09-30 pasó
al 1% del total y el 2026-10-01 Sara lo subió al 1.5%.

`tests/test_curva_respaldo_a_reportar.py` prueba la copia de `app/` (apagada);
este archivo prueba la que corre, en `apps/`.
"""
from types import SimpleNamespace

import pytest

from apps.energia.services.reporte.utils import (
    TOLERANCIA_RESPALDO_REAL_PCT,
    actualizar_respaldo_final,
    curva_respaldo_a_reportar,
)


def _rep(curva_final, curva_medidor_respaldo, medidor_usado="principal"):
    return SimpleNamespace(
        curva_final=curva_final,
        medidor_usado=medidor_usado,
        curva_respaldo_terceros=None,
        curva_medidor_respaldo=curva_medidor_respaldo,
    )


def _con_total(total):
    """Curva solar de 24 horas que suma `total`: todo entre las 6 y las 17."""
    return [0.0] * 6 + [total / 12] * 12 + [0.0] * 6


def test_la_tolerancia_es_el_uno_y_medio_por_ciento():
    assert TOLERANCIA_RESPALDO_REAL_PCT == 0.015


@pytest.mark.parametrize("factor", [1.014, 0.986, 1.015, 0.985, 1.012, 0.988])
def test_generacion_grande_dentro_del_1_5_por_ciento_usa_el_medidor(factor):
    # 2000 kWh: margen de 30 kWh. Con el 1% (20 kWh), 1.2% caía a estimado;
    # con el margen fijo de 1.5 kWh, casi todo.
    respaldo = _con_total(2000 * factor)
    curva, origen = curva_respaldo_a_reportar(_rep(_con_total(2000), respaldo))
    assert origen == "medidor"
    assert curva == respaldo


@pytest.mark.parametrize("factor", [1.0152, 0.9848])
def test_generacion_grande_fuera_del_1_5_por_ciento_es_estimado(factor):
    rep = _rep(_con_total(2000), _con_total(2000 * factor))
    assert curva_respaldo_a_reportar(rep)[1] == "estimado"


def test_consumo_chico_usa_la_misma_regla():
    # 48 kWh: 1.5% son 0.72 kWh. Con el margen fijo viejo, 1.2 kWh (2.5%) pasaba.
    assert curva_respaldo_a_reportar(_rep([2.0] * 24, [2.0] * 23 + [2.4]))[1] == "medidor"
    assert curva_respaldo_a_reportar(_rep([2.0] * 24, [2.0] * 23 + [2.7]))[1] == "medidor"
    assert curva_respaldo_a_reportar(_rep([2.0] * 24, [2.0] * 23 + [2.8]))[1] == "estimado"
    assert curva_respaldo_a_reportar(_rep([2.0] * 24, [2.0] * 23 + [3.2]))[1] == "estimado"


def test_cgm_tambien_compara_con_la_misma_regla():
    rep = _rep(_con_total(1000), _con_total(1008), medidor_usado="cgm")
    assert curva_respaldo_a_reportar(rep)[1] == "medidor"


def test_principal_en_cero_solo_acepta_respaldo_en_cero():
    assert curva_respaldo_a_reportar(_rep([0.0] * 24, [0.0] * 24))[1] == "medidor"
    assert curva_respaldo_a_reportar(_rep([0.0] * 24, [0.0] * 23 + [0.1]))[1] == "estimado"


def test_respaldo_desconectado_sigue_cayendo_a_estimado():
    rep = _rep(_con_total(2000), _con_total(150))
    assert curva_respaldo_a_reportar(rep)[1] == "estimado"


def test_actualizar_respaldo_final_guarda_el_origen_nuevo():
    rep = _rep(_con_total(2000), _con_total(2015))
    actualizar_respaldo_final(rep)
    assert rep.respaldo_final_origen == "medidor"
    assert rep.curva_respaldo_final == _con_total(2015)
