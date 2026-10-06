"""GaiaClient._get_con_estado()/get_border_report_status_con_estado() --
núcleo thread-safe que reemplaza depender de self.ultima_llamada_fallo
(compartida entre hilos, insegura bajo ThreadPoolExecutor -- ver
reporte_cgm.py, auditoría CGM 2026-08-26, finding #1).

_get()/get_border_report_status() (los wrappers históricos) deben seguir
comportándose igual que antes para todo el código secuencial existente."""
import threading
import types

import pytest

from apps.comun.integraciones.gaia_client import GaiaClient


def _client_con_http(get_fn):
    client = GaiaClient()
    client._access_token = "token-valido"
    client._token_time = __import__("time").time()
    client._http = types.SimpleNamespace(get=get_fn)
    return client


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_get_con_estado_exito_no_falla():
    client = _client_con_http(lambda *a, **kw: _Resp(200, {"a": 1}))
    data, fallo = client._get_con_estado("http://x")
    assert data == {"a": 1}
    assert fallo is False


def test_get_con_estado_404_no_es_fallo():
    """404 real de Quoia (recurso no existe) no es lo mismo que no poder preguntar."""
    client = _client_con_http(lambda *a, **kw: _Resp(404))
    data, fallo = client._get_con_estado("http://x")
    assert data is None
    assert fallo is False


def test_get_con_estado_excepcion_de_red_es_fallo():
    def _get(*a, **kw):
        raise RuntimeError("timeout")
    client = _client_con_http(_get)
    data, fallo = client._get_con_estado("http://x")
    assert data is None
    assert fallo is True


def test_get_wrapper_historico_sigue_actualizando_bandera_compartida():
    client = _client_con_http(lambda *a, **kw: _Resp(200, {"a": 1}))
    client.ultima_llamada_fallo = True
    data = client._get("http://x")
    assert data == {"a": 1}
    assert client.ultima_llamada_fallo is False


def test_get_border_report_status_con_estado_encuentra_la_fecha():
    payload = {
        "results": [
            {"report_date": "2026-08-25", "status": "OK"},
            {"report_date": "2026-08-24", "status": "OK"},
        ],
        "next": None,
    }
    client = _client_con_http(lambda *a, **kw: _Resp(200, payload))
    reporte, fallo = client.get_border_report_status_con_estado(123, "2026-08-25")
    assert reporte["status"] == "OK"
    assert fallo is False


def test_get_border_report_status_con_estado_sin_reporte_esa_fecha_no_es_fallo():
    payload = {"results": [{"report_date": "2026-08-24", "status": "OK"}], "next": None}
    client = _client_con_http(lambda *a, **kw: _Resp(200, payload))
    reporte, fallo = client.get_border_report_status_con_estado(123, "2026-08-25")
    assert reporte is None
    assert fallo is False


def test_get_border_report_status_con_estado_fallo_de_red_marca_fallo():
    def _get(*a, **kw):
        raise RuntimeError("connection reset")
    client = _client_con_http(_get)
    reporte, fallo = client.get_border_report_status_con_estado(123, "2026-08-25")
    assert reporte is None
    assert fallo is True


def test_get_border_report_status_wrapper_historico_descarta_fallo():
    payload = {"results": [{"report_date": "2026-08-25", "status": "WARNING"}], "next": None}
    client = _client_con_http(lambda *a, **kw: _Resp(200, payload))
    reporte = client.get_border_report_status(123, "2026-08-25")
    assert reporte["status"] == "WARNING"


def test_get_con_estado_es_local_no_pisa_entre_hilos():
    """Dos hilos, uno que falla y otro que tiene éxito, sobre el MISMO
    cliente -- el resultado de cada uno no debe verse afectado por el otro
    (a diferencia de self.ultima_llamada_fallo, que un hilo puede resetear
    antes de que el otro termine)."""
    resultados = {}

    def _get(url, headers=None, params=None):
        if "falla" in url:
            raise RuntimeError("boom")
        return _Resp(200, {"ok": True})

    client = _client_con_http(_get)

    def _llamar(nombre, url):
        data, fallo = client._get_con_estado(url)
        resultados[nombre] = (data, fallo)

    hilos = [
        threading.Thread(target=_llamar, args=("falla", "http://falla")),
        threading.Thread(target=_llamar, args=("ok", "http://ok")),
    ]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert resultados["falla"] == (None, True)
    assert resultados["ok"] == ({"ok": True}, False)
