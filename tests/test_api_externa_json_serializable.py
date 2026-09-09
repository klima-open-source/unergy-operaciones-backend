"""Lo que se manda a la API de Liquidaciones tiene que ser JSON serializable.

Bug real (2026-09-09): crear un contrato de energía devolvía un 500 con la
página HTML de Django, y **no se creaba nada**.

DRF entrega `validated_data` con tipos de Python, no con texto: un
`serializers.DateField()` produce un `datetime.date`. Ese dict se pasaba tal
cual a httpx, que serializa con `json.dumps` **sin `default=`** y lanza:

    TypeError: Object of type date is not JSON serializable

No es un `httpx.HTTPError`, así que no lo atrapaba ninguno de los `except` del
cliente —todos traducen a `LiquidacionesAPIError`, que sale como 502— y se
escapaba de DRF hasta convertirse en el 500 mudo. Reventaba **antes** de salir a
la red: de ahí que la API nunca viera la petición.

Lo mismo aplica a `Decimal`, que es lo que produce un `DecimalField` y que
`json.dumps` tampoco sabe serializar.
"""
import datetime
from decimal import Decimal

import pytest

pytest.importorskip("httpx")


def _seguro():
    from apps.liquidaciones.services.api_externa import _json_seguro

    return _json_seguro


def test_las_fechas_salen_como_texto_iso():
    """`date` y `datetime` son lo que produce un DateField de DRF."""
    fn = _seguro()
    assert fn({"date_from": datetime.date(2026, 8, 1)}) == {"date_from": "2026-08-01"}
    assert fn({"t": datetime.datetime(2026, 8, 1, 13, 45)})["t"].startswith("2026-08-01T13:45")


def test_los_decimales_salen_como_numero():
    assert _seguro()({"valor": Decimal("1200.50")}) == {"valor": 1200.5}


def test_no_toca_lo_que_ya_era_serializable():
    fn = _seguro()
    datos = {"code": "C1234", "company": 61, "percentage": 1.0, "activo": True, "x": None}
    assert fn(datos) == datos


def test_entra_en_listas_y_diccionarios_anidados():
    """Las cantidades (`hours`) y los proyectos van anidados."""
    fn = _seguro()
    entrada = {"proyectos": [{"desde": datetime.date(2026, 1, 1)}], "hours": [Decimal("1.5")]}
    assert fn(entrada) == {"proyectos": [{"desde": "2026-01-01"}], "hours": [1.5]}


def test_el_resultado_pasa_por_json_dumps_de_verdad():
    """La prueba que importa: httpx usa esto y no perdona."""
    import json

    fn = _seguro()
    payload = {
        "date_from": datetime.date(2026, 8, 1),
        "date_to": datetime.date(2026, 8, 4),
        "contract_type": "no_contract",
        "percentage": Decimal("1"),
    }
    json.dumps(fn(payload))  # sin `default=`, igual que httpx


def test_httpx_acepta_el_payload_ya_convertido():
    """Se arma la petición de verdad: es donde saltaba el TypeError."""
    import httpx

    fn = _seguro()
    httpx.Request(
        "POST", "https://example.invalid/x",
        json=fn({"date_from": datetime.date(2026, 8, 1), "valor": Decimal("2.5")}),
    )
