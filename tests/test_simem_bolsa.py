"""Conector SIMEM del precio de bolsa. Funciones puras: sin BD, sin red, sin reloj.
El fetch se prueba con httpx.MockTransport (sin red real)."""
from apps.comun.integraciones.simem_bolsa import (
    _version_rank,
    promedio_diario_max_version,
    promedio_ultimos_n_dias,
)


def _rec(var, fechahora, version, valor):
    return {
        "CodigoVariable": var, "FechaHora": fechahora, "CodigoDuracion": "PT1H",
        "UnidadMedida": "COP/kWh", "Version": version, "Valor": valor,
    }


def test_version_rank_ordena_tx_numericas_y_finales():
    assert _version_rank("TX1") < _version_rank("TX2")
    assert _version_rank("TX2") < _version_rank("TXR")
    assert _version_rank("TXR") < _version_rank("TXF")
    # desconocida no rompe: cae al fondo
    assert _version_rank("???") < _version_rank("TX1")


def test_promedio_diario_toma_version_mas_alta_por_dia():
    # Día 01: TX1 (valor 100) y TX2 (valor 200) -> gana TX2 = 200
    # Día 02: solo TX1 (valor 300) -> 300
    recs = [
        _rec("PB_Nal", "2026-08-01 00:00:00", "TX1", 100.0),
        _rec("PB_Nal", "2026-08-01 01:00:00", "TX2", 200.0),
        _rec("PB_Nal", "2026-08-02 00:00:00", "TX1", 300.0),
    ]
    out = promedio_diario_max_version(recs)
    assert out == {"2026-08-01": 200.0, "2026-08-02": 300.0}


def test_promedio_diario_filtra_solo_pb_nal():
    recs = [
        _rec("PB_Nal", "2026-08-01 00:00:00", "TX1", 100.0),
        _rec("PB_Int", "2026-08-01 00:00:00", "TX1", 999.0),
        _rec("PB_Tie", "2026-08-01 00:00:00", "TX1", 888.0),
    ]
    out = promedio_diario_max_version(recs)
    assert out == {"2026-08-01": 100.0}


def test_promedio_diario_promedia_las_horas_del_dia():
    recs = [
        _rec("PB_Nal", "2026-08-01 00:00:00", "TX1", 100.0),
        _rec("PB_Nal", "2026-08-01 01:00:00", "TX1", 200.0),
    ]
    out = promedio_diario_max_version(recs)
    assert out == {"2026-08-01": 150.0}


def test_promedio_ultimos_n_dias_toma_los_mas_recientes():
    daily = {f"2026-08-0{d}": float(d) for d in range(1, 9)}  # 1..8
    # últimos 7 días conocidos = 02..08 -> promedio de 2,3,4,5,6,7,8 = 5.0
    assert promedio_ultimos_n_dias(daily, 7) == 5.0


def test_promedio_ultimos_n_dias_vacio_devuelve_none():
    assert promedio_ultimos_n_dias({}, 7) is None


import httpx
from apps.comun.integraciones.simem_bolsa import fetch_records, DATASET_PRECIO_BOLSA


def test_fetch_records_arma_url_y_parsea_result_records():
    capturado = {}

    def handler(request):
        capturado["url"] = str(request.url)
        return httpx.Response(200, json={"result": {"records": [
            {"CodigoVariable": "PB_Nal", "FechaHora": "2026-08-01 00:00:00",
             "Version": "TX1", "Valor": 100.0},
        ]}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    recs = fetch_records("2026-08-01", "2026-08-14", client=client)

    assert f"datasetId={DATASET_PRECIO_BOLSA}" in capturado["url"]
    assert "startdate=2026-08-01" in capturado["url"]
    assert "enddate=2026-08-14" in capturado["url"]
    assert len(recs) == 1 and recs[0]["Valor"] == 100.0


def test_fetch_records_sin_records_devuelve_lista_vacia():
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})))
    assert fetch_records("2026-08-01", "2026-08-14", client=client) == []


from apps.comun.integraciones.simem_bolsa import precio_bolsa_prom_7d


def test_precio_bolsa_prom_7d_integra_fetch_agregado_y_promedio():
    # 8 días, PB_Nal, una hora por día, valor = número de día. Dos versiones el día 1.
    recs = []
    for d in range(1, 9):
        recs.append({"CodigoVariable": "PB_Nal", "FechaHora": f"2026-08-0{d} 00:00:00",
                     "Version": "TX1", "Valor": float(d)})
    recs.append({"CodigoVariable": "PB_Nal", "FechaHora": "2026-08-01 00:00:00",
                 "Version": "TX2", "Valor": 100.0})  # gana TX2 el día 1, pero cae fuera de los últimos 7

    client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"result": {"records": recs}})))
    # últimos 7 días conocidos = 02..08 -> promedio 5.0
    val = precio_bolsa_prom_7d("2026-08-01", "2026-08-14", n_dias=7, client=client)
    assert val == 5.0
