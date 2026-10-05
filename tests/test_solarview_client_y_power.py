"""SolarViewClient (Fase 1 de la migración de Solenium a SolarView) --
auth por token estático, y curva_de_power() sin el loop de suma manual por
inversor (con total_power=1 la API ya entrega la potencia sumada, ver
SolarViewClient.get_power)."""
import pandas as pd

from apps.comun.integraciones import solarview_client as sv_client_module
from apps.comun.integraciones.solarview_client import SolarViewClient
from app.services.reporte_energia.solarview import curva_de_power


def _cliente(token="abc123"):
    client = SolarViewClient.__new__(SolarViewClient)
    client._base_url = "https://api.sole.tech"
    client._token = token
    return client


def test_headers_usan_token_estatico_sin_bearer():
    client = _cliente()
    assert client._headers() == {"Authorization": "Token abc123"}


def test_enabled_es_falso_sin_token():
    client = _cliente(token="")
    assert client.enabled is False


def test_get_power_pide_total_power_1():
    client = _cliente()
    llamados = []
    client._get = lambda url, params=None: (llamados.append((url, params)) or {"results": {}})

    client.get_power(7, "2026-08-20", "2026-08-20")

    url, params = llamados[0]
    assert url.endswith("/solarview/measurements/power/")
    assert params["total_power"] == 1
    assert params["project_id"] == 7


def test_get_company_projects_pide_la_ruta_correcta_y_parsea_results():
    client = _cliente()
    llamados = []
    client._get = lambda url, params=None: (
        llamados.append((url, params)) or {
            "message": "OK", "error": None, "success": True,
            "results": [{"id": 103, "name": "Minigranja Solar El Prado"}],
        }
    )

    proyectos = client.get_company_projects()

    url, params = llamados[0]
    assert url.endswith("/solarview/config/company-projects/")
    assert params is None
    assert proyectos == [{"id": 103, "name": "Minigranja Solar El Prado"}]


def test_get_company_projects_vacio_si_falla_la_consulta():
    client = _cliente()
    client._get = lambda url, params=None: None

    assert client.get_company_projects() == []


def test_get_relay_historical_usa_recloser_como_nombre_de_parametro():
    """El parámetro se llama 'recloser' pero recibe el project_id (mismo
    gotcha que ya existía en la API vieja) -- ver plan de migración."""
    client = _cliente()
    llamados = []
    client._get = lambda url, params=None: (llamados.append((url, params)) or {"results": {}})

    client.get_relay_historical(7, "2026-08-20 00:00:00", "2026-08-20 23:59:59")

    url, params = llamados[0]
    assert url.endswith("/solarview/config/recloser/historical/")
    assert params["recloser"] == 7
    assert params["start_date"] == "2026-08-20 00:00:00"


def test_curva_de_power_ya_viene_sumada_sin_loop_por_inversor():
    """Con total_power=1, resp['results']['power'] es {ts: kw} plano --
    a diferencia de la API vieja de Solenium ({inversor: {ts: kw}})."""
    resp = {
        "message": "OK", "error": None, "success": True,
        "results": {
            "unit": "kW",
            "power": {
                "2026-08-20 10:00": 100.0,
                "2026-08-20 11:00": 200.0,
            },
        },
    }
    curva, horas_con_dato = curva_de_power(resp)

    assert horas_con_dato == {10, 11}
    # Riemann con 1h de separación entre los dos puntos: 100 kW * 1h = 100 kWh en la hora 10
    assert curva[10] == 100.0


def test_curva_de_power_vacia_si_no_hay_power():
    curva, horas_con_dato = curva_de_power({"results": {}})
    assert horas_con_dato == set()
    assert curva.isna().all()


def test_curva_de_power_descarta_hora_implausible():
    """Mismo criterio que curva_generacion() (ver limite_plausible_kwh()
    en utils.py) -- acá el riesgo es mayor: curva_de_power() se reporta
    DIRECTO como curva_final en el rescate de Caso 6/7, sin ningún FP ni
    comparación de por medio."""
    resp = {
        "results": {
            "power": {
                "2026-08-20 10:00": 500.0,      # 500 kWh en 1h -- plausible
                "2026-08-20 11:00": 250000.0,   # implausible para 0,99 MW
            },
        },
    }
    curva, horas_con_dato = curva_de_power(resp, capacidad_efectiva_mw=0.99)

    assert curva[10] == 500.0
    assert curva[11] is None or curva[11] != curva[11]  # NaN -- descartada
    assert 11 not in horas_con_dato


def test_curva_de_power_sin_capacidad_efectiva_no_filtra():
    resp = {
        "results": {
            "power": {
                "2026-08-20 11:00": 250000.0,
                "2026-08-20 12:00": 250000.0,
            },
        },
    }
    curva, _ = curva_de_power(resp)
    assert curva[11] == 250000.0


class _RespuestaFalsa:
    def __init__(self, status_code, headers=None, data=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._data = data or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_reintenta_ante_429_con_pausa(monkeypatch):
    client = _cliente()
    respuestas = [
        _RespuestaFalsa(429, headers={"Retry-After": "5"}),
        _RespuestaFalsa(200, data={"ok": True}),
    ]
    client._http = type("_HttpFalso", (), {"get": lambda self, url, headers=None, params=None: respuestas.pop(0)})()

    esperas = []
    monkeypatch.setattr(sv_client_module.time, "sleep", lambda s: esperas.append(s))

    resultado = client._get("https://api.sole.tech/solarview/measurements/generation/")

    assert resultado == {"ok": True}
    assert esperas == [5.0]  # respeta Retry-After en vez de la pausa fija


def test_429_sin_retry_after_usa_pausa_fija(monkeypatch):
    client = _cliente()
    respuestas = [_RespuestaFalsa(429), _RespuestaFalsa(200, data={"ok": True})]
    client._http = type("_HttpFalso", (), {"get": lambda self, url, headers=None, params=None: respuestas.pop(0)})()

    esperas = []
    monkeypatch.setattr(sv_client_module.time, "sleep", lambda s: esperas.append(s))

    client._get("https://api.sole.tech/solarview/measurements/generation/")

    assert esperas == [sv_client_module.BACKOFF_SECONDS]


# ── Fase 2: los cuatro metodos que faltaban para poder portar
# app/api/v1/generacion_solar.py de Solenium a SolarView. Los contratos se
# verificaron en vivo contra api.sole.tech el 2026-09-03 (ver docstrings).

def test_get_project_detail_pide_la_ruta_con_el_id_en_el_path():
    client = _cliente()
    llamados = []
    client._get = lambda url, params=None: (llamados.append((url, params)) or {"results": {}})

    client.get_project_detail(12)

    url, params = llamados[0]
    assert url.endswith("/solarview/config/project-detail/12/"), (
        "el project_id va en el path, no como parametro"
    )
    assert params is None


def test_get_project_inverters_desenvuelve_results_a_lista():
    client = _cliente()
    llamados = []
    client._get = lambda url, params=None: (
        llamados.append((url, params)) or {
            "message": "OK", "error": None, "success": True,
            "results": [
                {"id": 722, "dev_name": "INV-01", "state": 1, "power": 84.3,
                 "efficiency": 97.1, "temperature": 41.0, "time": "2026-09-03 15:30:00"},
                {"id": 723, "dev_name": "INV-02", "state": 1, "power": 81.9,
                 "efficiency": 96.8, "temperature": 40.2, "time": "2026-09-03 15:30:00"},
            ],
        }
    )

    inversores = client.get_project_inverters(12)

    url, params = llamados[0]
    assert url.endswith("/solarview/measurements/inverters-list/")
    assert params == {"project_id": 12}
    assert [i["id"] for i in inversores] == [722, 723]


def test_get_project_inverters_devuelve_lista_vacia_si_la_forma_no_es_la_esperada():
    client = _cliente()
    client._get = lambda url, params=None: None
    assert client.get_project_inverters(12) == []

    client._get = lambda url, params=None: {"results": "no soy una lista"}
    assert client.get_project_inverters(12) == []


def test_get_inverter_detail_pide_solo_el_id_del_inversor():
    """La firma NO coincide con la de Solenium, que pide (project_id,
    inverter_id) -- aca el inversor se identifica solo por su id."""
    client = _cliente()
    llamados = []
    client._get = lambda url, params=None: (llamados.append((url, params)) or {"results": {}})

    client.get_inverter_detail(722)

    url, params = llamados[0]
    assert url.endswith("/solarview/measurements/inverter-detail/")
    assert params == {"id": 722}


def test_get_energy_convierte_mwh_a_kwh_aunque_la_clave_se_llame_kwh():
    """El endpoint declara unit "MWh" y aun asi nombra la clave `kwh` -- el
    nombre miente por un factor de 1000. Caso real verificado en vivo
    (project_id=11, Baraya): 7.7266 "kwh" con unit MWh son 7.726,6 kWh.
    """
    client = _cliente()
    client._get = lambda url, params=None: {
        "message": "OK", "error": None, "success": True,
        "results": {
            "project_id": 11, "granularity": "day",
            "date_from": "2026-08-01", "date_to": "2026-09-03",
            "unit": "MWh",
            "points": [
                {"time": "2026-08-01", "kwh": 7.7266200000000005},
                {"time": "2026-08-02", "kwh": 6.92509875},
                {"time": "2026-08-03", "kwh": None},
            ],
        },
    }

    r = (client.get_energy(11, date_from="2026-08-01", date_to="2026-09-03") or {})["results"]

    assert r["unit"] == "kWh", "al salir del cliente la unidad siempre es kWh"
    assert r["points"][0]["kwh"] == 7726.62
    assert r["points"][1]["kwh"] == 6925.099
    assert r["points"][2]["kwh"] is None, "un punto sin dato se deja como esta, no se convierte a 0"


def test_get_energy_no_toca_los_valores_si_ya_vienen_en_kwh():
    client = _cliente()
    client._get = lambda url, params=None: {
        "results": {"unit": "kWh", "points": [{"time": "2026-08-01", "kwh": 7726.62}]},
    }

    r = (client.get_energy(11, date_from="2026-08-01", date_to="2026-08-01") or {})["results"]

    assert r["unit"] == "kWh"
    assert r["points"][0]["kwh"] == 7726.62, "no se debe multiplicar dos veces"


def test_get_energy_pasa_las_fechas_que_el_endpoint_exige():
    """Sin date_from/date_to la API responde points vacio -- verificado en
    vivo con tres proyectos distintos."""
    client = _cliente()
    llamados = []
    client._get = lambda url, params=None: (llamados.append((url, params)) or {"results": {}})

    client.get_energy(11, granularity="day", date_from="2026-08-01", date_to="2026-09-03")

    url, params = llamados[0]
    assert url.endswith("/solarview/measurements/energy/")
    assert params == {"project_id": 11, "granularity": "day",
                      "date_from": "2026-08-01", "date_to": "2026-09-03"}


def test_get_energy_tolera_una_respuesta_inesperada():
    client = _cliente()
    client._get = lambda url, params=None: None
    assert client.get_energy(11) is None

    client._get = lambda url, params=None: {"results": None}
    assert client.get_energy(11) == {"results": None}


def test_get_power_por_inversor_con_total_power_cero():
    """`total_power` cambia la FORMA de la respuesta, no solo su contenido:
    con 1 la API entrega {ts: kw} ya sumado, con 0 entrega
    {nombre_inversor: {ts: kw}}. Verificado en vivo el 2026-09-03.

    Pedir el default cuando se querian series por inversor devolvia numeros
    donde el llamador esperaba diccionarios -- y una lista vacia sin error."""
    client = _cliente()
    llamados = []
    client._get = lambda url, params=None: (llamados.append((url, params)) or {"results": {}})

    client.get_power(11, "2026-09-03", "2026-09-03", total_power=0)

    assert llamados[0][1]["total_power"] == 0


def test_get_power_suma_el_proyecto_por_defecto():
    client = _cliente()
    llamados = []
    client._get = lambda url, params=None: (llamados.append((url, params)) or {"results": {}})

    client.get_power(11, "2026-09-03", "2026-09-03")

    assert llamados[0][1]["total_power"] == 1


# ── Aviso de forma inesperada ────────────────────────────────────────────────
# `total_power` decide la ESTRUCTURA de la respuesta, y leer la que no es no
# lanza ninguna excepcion: el llamador descarta todo y devuelve vacio. Eso paso
# dos veces en la migracion (la curva del detalle y /inverters-power) y tardo
# dias en verse. El warning convierte el fallo silencioso en una senal.

def _power(payload):
    return {"results": {"unit": "kW", "power": payload}}


def test_avisa_si_pidio_sumado_y_llego_por_inversor(caplog):
    client = _cliente()
    client._get = lambda url, params=None: _power({"INV-1": {"08:00": 110.0}})

    client.get_power(11, total_power=1)

    assert "forma contraria" in caplog.text


def test_avisa_si_pidio_por_inversor_y_llego_sumado(caplog):
    client = _cliente()
    client._get = lambda url, params=None: _power({"08:00": 218.0})

    client.get_power(11, total_power=0)

    assert "forma contraria" in caplog.text


def test_no_avisa_cuando_la_forma_es_la_correcta(caplog):
    client = _cliente()
    client._get = lambda url, params=None: _power({"08:00": 218.0})
    client.get_power(11, total_power=1)

    client._get = lambda url, params=None: _power({"INV-1": {"08:00": 110.0}})
    client.get_power(11, total_power=0)

    assert "forma contraria" not in caplog.text


def test_no_avisa_ante_una_respuesta_vacia_o_rara(caplog):
    """Sin datos no hay forma que validar -- eso ya lo maneja el llamador."""
    client = _cliente()
    for payload in (None, {}, {"results": None}, _power({})):
        client._get = lambda url, params=None, p=payload: p
        client.get_power(11, total_power=1)

    assert "forma contraria" not in caplog.text
