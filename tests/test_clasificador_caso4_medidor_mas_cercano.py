"""Caso 4: se reporta el medidor más cercano a inversores, no el más alto.

Cuando ningún medidor pasa el ±6% del Caso 2 y los medidores quedan POR ENCIMA
de los inversores, el árbol reporta un medidor directo. Antes reportaba el de
mayor energía; desde 2026-09-21 reporta el más cercano a inversores de entre
los que están por encima (decisión de la usuaria).

El día que lo motivó: inversores completos en 4.346,9 kWh, principal en
4.617,6 kWh (-6,2%, se pasó del rango por 0,23 puntos) y respaldo en 6.242,3
kWh (+43%). Se reportaba el respaldo, que era el dato claramente malo.

Estas pruebas van contra `apps.energia.services.reporte.clasificador` -- el
que corre en producción. Las otras `test_clasificador_*` todavía apuntan a la
copia de `app/`, que no se sirve.
"""
import os
from datetime import date

import pandas as pd
import pytest

# El clasificador de `apps/` importa modelos de Django, así que las settings
# tienen que estar listas ANTES del import -- mismo arranque que usan los otros
# tests del árbol Django (ver tests/test_reporte_energia_automatico.py).
django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("SECRET_KEY", "x" * 40)
django.setup()

from apps.energia.services.reporte import clasificador, historial  # noqa: E402

FECHA = date(2026, 9, 21)
FECHA_STR = str(FECHA)

E_INV = 4346.9
E_PPAL = 4617.6
E_RESP = 6242.3


def _curva(total_dia: float) -> pd.Series:
    """24h repartiendo `total_dia` en la ventana solar (6-17), 0 fuera."""
    return pd.Series(
        {h: (total_dia / 12 if 6 <= h < 18 else 0.0) for h in range(24)},
        dtype=float,
    )


CURVA_SIN_DATO = pd.Series({h: None for h in range(24)}, dtype=float)


def _decidir(curva_ppal: pd.Series, curva_resp: pd.Series) -> dict:
    """Sin reporte CGM para el día e inversores completos -- la rama de
    Casos 2/3/4."""
    return clasificador._decidir_caso(
        frontera_id=1, fecha=FECHA, fecha_str=FECHA_STR,
        e_cgm=0.0, curva_cgm=_curva(0.0), reporte_valido=False,
        cgm_tiene_dato=False,
        curva_ppal=curva_ppal, curva_resp=curva_resp,
        completo_ppal=True, completo_resp=True,
        e_inv=E_INV, e_inv_incompleto=None, curva_solarview=_curva(E_INV),
        id_solarview=123, node_ppal=None, gaia=object(), sv=object(),
    )


def test_caso4_reporta_el_medidor_mas_cercano_a_inversores():
    resultado = _decidir(_curva(E_PPAL), _curva(E_RESP))

    assert resultado["caso"] == 4
    assert resultado["medidor_usado"] == "principal"
    assert resultado["energia_final_kwh"] == pytest.approx(E_PPAL)


def test_caso4_no_se_marca_para_revision_manual():
    """El día llega al Caso 4 por fallar el ±6%, muchas veces por décimas --
    marcarlo llenaría la revisión de días casi correctos."""
    resultado = _decidir(_curva(E_PPAL), _curva(E_RESP))

    assert resultado.get("revisar_manualmente") is not True


def test_caso4_ignora_al_medidor_que_quedo_por_debajo():
    """'Más cercano' se busca SOLO entre los que superan a los inversores. Un
    medidor por debajo es la firma del Caso 3 (subreporta): aunque esté más
    cerca en valor absoluto, reportarlo sería reportar un número que ya se
    sabe bajo. Acá el principal está 10% por debajo (434,7 kWh de distancia,
    contra 1.895,4 del respaldo) y aun así se reporta el respaldo. El 10% es
    a propósito: con menos se metería dentro del ±6% y saldría Caso 2, sin
    llegar nunca a esta decisión."""
    e_ppal_bajo = E_INV * 0.90

    resultado = _decidir(_curva(e_ppal_bajo), _curva(E_RESP))

    assert resultado["caso"] == 4
    assert resultado["medidor_usado"] == "respaldo"
    assert resultado["energia_final_kwh"] == pytest.approx(E_RESP)


def test_caso4_con_un_solo_medidor_lo_reporta_igual():
    """Sin respaldo con dato, el único candidato por encima es el principal --
    mismo resultado que antes del cambio."""
    resultado = _decidir(_curva(E_RESP), CURVA_SIN_DATO)

    assert resultado["caso"] == 4
    assert resultado["medidor_usado"] == "principal"
    assert resultado["energia_final_kwh"] == pytest.approx(E_RESP)


def test_caso3_sigue_decidiendose_igual(monkeypatch):
    """Los dos medidores por debajo de inversores -> sigue siendo Caso 3. El
    cambio no mueve días entre casos: esa frontera la decide _mejor_medidor,
    y "el mayor está por encima" equivale a "hay al menos uno por encima"."""
    monkeypatch.setattr(
        historial, "get_factor_perdida_detalle", lambda fid, fecha: (0.97, 0.97)
    )

    resultado = _decidir(_curva(E_INV * 0.80), _curva(E_INV * 0.85))

    assert resultado["caso"] == 3
