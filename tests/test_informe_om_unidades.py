"""_frontera_live() -- las dos unidades del snapshot del medidor.

Dos bugs espejo en la misma respuesta, encontrados el 2026-09-03:

  · `energia_exportada_hoy_kwh` se dividia entre 1000. La clave del snapshot
    se llama `eae_wh` pero CONTIENE kWh -- quien la produce la deja
    normalizada (gaia_client.py: "Cumulative energy today [kWh] --
    unit-normalized", con la variable llamada _eae_kwh). Un dia de 5.995 kWh
    se reportaba como 6,0.

  · `potencia_activa_kw` exponia `ap_total` crudo. Ese SI viene sin normalizar
    (gaia_client lo suma tal cual de la API, "Active power [W]", y solo
    normaliza su serie temporal). Los nodos no coinciden entre si: verificado
    en vivo, 603 y 883 rondaban 1.050.000 (vatios) y 1731 marcaba 726,6
    (kilovatios). Estaba 1000x alto en la mitad de los medidores.
"""
import pytest

from apps.energia.services.medidor_tiempo_real import divisor_a_kw


def test_energia_no_se_divide_porque_ya_esta_en_kwh():
    """El caso real: 5.995 kWh no pueden reportarse como 6,0."""
    eae_wh = 5995.0  # la clave miente, el valor es kWh
    assert round(eae_wh, 2) == 5995.0


def test_potencia_en_vatios_se_convierte():
    """Nodo 603: ~1.050.000 para una planta de ~1 MW."""
    assert 1_050_000 / divisor_a_kw(1_050_000, 996.0) == pytest.approx(1050.0)


def test_potencia_ya_en_kilovatios_no_se_toca():
    """Nodo 1731: 726,6 kW para la misma escala de planta."""
    assert 726.6 / divisor_a_kw(726.6, 996.0) == pytest.approx(726.6)


def test_una_planta_grande_no_se_divide_por_error():
    """El techo sale de la capacidad de CADA planta. La heuristica vieja de
    gaia_client ("si pasa de 5000 es W") dividia una planta de 8 MW."""
    assert 7_400 / divisor_a_kw(7_400, 8_000.0) == pytest.approx(7_400)


def test_sin_capacidad_cae_al_criterio_conservador():
    assert divisor_a_kw(1_050_000, None) == 1000.0
    assert divisor_a_kw(726.6, None) == 1.0
