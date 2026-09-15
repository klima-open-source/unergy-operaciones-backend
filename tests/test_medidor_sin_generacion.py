"""Un medidor que no exportó energía no dibuja curva de generación.

Reportado el 2026-09-15 sobre **MGS 0007 La Paz Vallenata**: la tarjeta mostraba
una meseta plana de ~740 kW en el medidor, **incluida la madrugada**, con los
inversores en cero y "0 kWh" escrito arriba en la misma tarjeta. En Quoia no
había energía exportada. La tarjeta se contradecía sola.

La causa es que Quoia entrega la potencia activa **con signo** --generar es
negativo, consumir positivo-- y `snapshot_medidor` la publica en valor absoluto
(`abs(...)`). Una planta parada que consume queda dibujada igual que una
produciendo a tope.

Medido ese día contra producción:

    Valencia Oriente 1   ap -711,5   eae 3.539 kWh    generando
    Agustín 1            ap -968,0   eae 6.124 kWh    generando
    Taurus IX            ap   +2,2   eae     0 kWh    parada
    Cacica               ap   +1,6   eae     0 kWh    parada
    Vallenata            ap +740,9   eae     0 kWh    parada

**El árbitro es la energía, no el signo.** Si el contador del día quedó en cero
no hubo generación, y no hay curva que dibujar. No se decide por el signo porque
esa convención se comprobó en cinco plantas de UN día: poco para apoyar un
criterio que decide qué se ve y qué no.

`energia_kwh = None` es otra cosa: el canal de energía no reportó y no se puede
probar que no generó. Los dos canales se caen por separado -- ver el nodo 1693
en `_horas_cubiertas`, con `ap` todo en cero mientras `eae` sí acumulaba.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _limpiar(snap):
    from apps.energia.services.solarview_monitoreo import _sin_curva_si_no_genero

    return _sin_curva_si_no_genero(snap)


def _snap(energia, puntos=3, kw=740.0):
    return {
        "node_id": 845,
        "energia_kwh": energia,
        "energia_hasta": "2026-09-15T14:45:00-05:00",
        "curva": [{"time": f"2026-09-15T0{i}:15:00-05:00", "kw": kw} for i in range(puntos)],
    }


# ── El caso del reporte ─────────────────────────────────────────────────────


def test_vallenata_no_dibuja_curva():
    """0 kWh exportados: la meseta de 740 kW era consumo."""
    limpio = _limpiar(_snap(energia=0.0))

    assert limpio["curva"] == []
    assert limpio["sin_generacion"] is True


def test_una_planta_generando_conserva_su_curva():
    limpio = _limpiar(_snap(energia=6124.0, puntos=61, kw=968.0))

    assert len(limpio["curva"]) == 61
    assert "sin_generacion" not in limpio


@pytest.mark.parametrize("energia", [0.0, 0])
def test_cualquier_forma_del_cero_cuenta_como_sin_generacion(energia):
    assert _limpiar(_snap(energia=energia))["curva"] == []


def test_un_solo_kwh_ya_es_generacion():
    """El umbral es "algo más que cero", no una cantidad mínima: una planta que
    arrancó tarde o que estuvo casi todo el día caída igual generó."""
    limpio = _limpiar(_snap(energia=0.5))

    assert limpio["curva"] != []


# ── Lo que NO se puede afirmar ──────────────────────────────────────────────


def test_sin_contador_la_curva_se_respeta():
    """`energia_kwh = None` es "el canal de energía no reportó", no "no generó".
    Los dos canales se caen por separado (nodo 1693: `ap` en cero con `eae`
    acumulando). Borrar acá seria esconder el único dato que quedó."""
    limpio = _limpiar(_snap(energia=None))

    assert len(limpio["curva"]) == 3
    assert "sin_generacion" not in limpio


def test_sin_medidor_no_inventa_nada():
    assert _limpiar(None) is None


def test_un_medidor_sin_curva_no_se_marca():
    """Ya no había nada que borrar: marcarlo diría que se comprobó algo que no
    se comprobó."""
    snap = {"node_id": 845, "energia_kwh": 0.0, "curva": []}

    assert "sin_generacion" not in _limpiar(snap)


# ── Que no mute lo que recibe ───────────────────────────────────────────────


def test_no_modifica_el_original():
    """`elegir_medidor` devuelve UNO de los dos diccionarios que recibe, y los
    tres van en la respuesta (`medidor`, `medidor_principal`, `medidor_respaldo`).
    Mutando, borrar la curva de uno la borraría en los otros."""
    original = _snap(energia=0.0)

    _limpiar(original)

    assert len(original["curva"]) == 3


def test_conserva_los_demas_campos():
    limpio = _limpiar(_snap(energia=0.0))

    assert limpio["node_id"] == 845
    assert limpio["energia_kwh"] == 0.0
    assert limpio["energia_hasta"] == "2026-09-15T14:45:00-05:00"


# ── El cableado ─────────────────────────────────────────────────────────────


def test_se_aplica_antes_de_elegir_el_medidor():
    """Si se aplicara después, `medidor_principal` y `medidor_respaldo`
    seguirían llevando la curva vieja y la tarjeta contaría dos historias."""
    import inspect

    from apps.energia.services import solarview_monitoreo

    fuente = inspect.getsource(solarview_monitoreo.monitoreo_detalle)
    limpieza = fuente.index("_sin_curva_si_no_genero")
    eleccion = fuente.index("elegir_medidor(med_p, med_r)")

    assert limpieza < eleccion


def test_los_dos_medidores_pasan_por_el_filtro():
    import inspect

    from apps.energia.services import solarview_monitoreo

    fuente = inspect.getsource(solarview_monitoreo.monitoreo_detalle)

    assert fuente.count("_sin_curva_si_no_genero(") == 2
