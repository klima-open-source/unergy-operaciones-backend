"""GaiaClient.get_border_reports_status_con_estado() -- varias fechas del
mismo border en una sola pasada paginada, en vez de una llamada HTTP por
fecha (auditoría CGM 2026-08-26, finding #4).

get_border_report_status_con_estado() (una sola fecha) ahora es un wrapper
sobre este método -- se verifica que su comportamiento no cambió."""
import types

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


def test_una_sola_pagina_resuelve_varias_fechas_con_una_sola_llamada():
    payload = {
        "results": [
            {"report_date": "2026-08-25", "status": "OK"},
            {"report_date": "2026-08-24", "status": "WARNING"},
            {"report_date": "2026-08-23", "status": "OK"},
        ],
        "next": None,
    }
    llamadas = []

    def _get(url, headers=None, params=None):
        llamadas.append(url)
        return _Resp(200, payload)

    client = _client_con_http(_get)
    encontrados, fallo = client.get_border_reports_status_con_estado(
        123, {"2026-08-25", "2026-08-24", "2026-08-22"},
    )

    assert len(llamadas) == 1  # una sola llamada HTTP para las 3 fechas pedidas
    assert fallo is False
    assert set(encontrados) == {"2026-08-25", "2026-08-24"}  # 08-22 no estaba
    assert encontrados["2026-08-24"]["status"] == "WARNING"


def test_para_de_paginar_en_cuanto_encuentra_todas_las_fechas_pedidas():
    pagina_1 = {"results": [{"report_date": "2026-08-25", "status": "OK"}], "next": "http://pagina2"}
    pagina_2 = {"results": [{"report_date": "2026-08-24", "status": "OK"}], "next": "http://pagina3"}
    llamadas = []

    def _get(url, headers=None, params=None):
        llamadas.append(url)
        if url == "http://pagina2":
            return _Resp(200, pagina_2)
        return _Resp(200, pagina_1)

    client = _client_con_http(_get)
    encontrados, fallo = client.get_border_reports_status_con_estado(123, {"2026-08-25", "2026-08-24"})

    assert len(llamadas) == 2  # no llegó a pedir pagina3 -- ya tenía las 2 fechas
    assert set(encontrados) == {"2026-08-25", "2026-08-24"}


def test_fecha_faltante_al_agotar_paginas_no_es_fallo():
    payload = {"results": [{"report_date": "2026-08-01", "status": "OK"}], "next": None}
    client = _client_con_http(lambda *a, **kw: _Resp(200, payload))
    encontrados, fallo = client.get_border_reports_status_con_estado(123, {"2026-08-25"})
    assert encontrados == {}
    assert fallo is False


def test_fallo_de_red_se_reporta_para_lo_que_quedo_sin_resolver():
    def _get(*a, **kw):
        raise RuntimeError("timeout")
    client = _client_con_http(_get)
    encontrados, fallo = client.get_border_reports_status_con_estado(123, {"2026-08-25"})
    assert encontrados == {}
    assert fallo is True


def test_get_border_report_status_con_estado_una_fecha_sigue_igual():
    payload = {"results": [{"report_date": "2026-08-25", "status": "OK"}], "next": None}
    client = _client_con_http(lambda *a, **kw: _Resp(200, payload))
    reporte, fallo = client.get_border_report_status_con_estado(123, "2026-08-25")
    assert reporte["status"] == "OK"
    assert fallo is False
