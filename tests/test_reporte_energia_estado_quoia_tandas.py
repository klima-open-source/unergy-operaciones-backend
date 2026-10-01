"""`estado_quoia_revisar` revisa por tandas y guarda cada fila al responder.

Caso real, 2026-09-30: después de enviar ~108 fronteras, el `POST
/estado-quoia` hacía una llamada a Quoia por frontera y pasaba del `--timeout
120` de gunicorn. El proceso moría antes del bulk_update del final, y el front
-- que ignora ese error en silencio -- nunca mostraba el panel de estado.

`tests/test_reporte_energia_estado_quoia.py` prueba la copia de `app/`
(apagada); este archivo prueba la que corre, en `apps/`. Sin base de datos:
los modelos y Quoia se reemplazan por dobles.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

FECHA = None


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()

    global FECHA
    from datetime import date

    FECHA = date(2026, 9, 30)


class _Fila:
    def __init__(self, codigo, verificado_en=None):
        self.frontera = SimpleNamespace(id=hash(codigo) % 1000, codigo_frontera=codigo,
                                        nombre_frontera=codigo, proyecto=None)
        self.xm_exitoso = None
        self.xm_estado = None
        self.xm_process_id = None
        self.xm_verificado_en = verificado_en
        self.guardada_con = None

    def save(self, update_fields=None):
        self.guardada_con = update_fields


class _Gaia:
    def __init__(self, respuestas=None, al_consultar=None):
        self.respuestas = respuestas or {}
        self.consultados = []
        self.al_consultar = al_consultar

    def get_border_report_status(self, border_id, fecha):
        self.consultados.append(border_id)
        if self.al_consultar:
            self.al_consultar(border_id)
        return self.respuestas.get(border_id)


def _preparar(monkeypatch, filas, gaia):
    from apps.energia.services.reporte import envio

    monkeypatch.setattr(envio, "_fronteras_enviadas",
                        lambda fecha: [(f, f.frontera, "generacion") for f in filas])
    monkeypatch.setattr(envio, "GaiaClient", lambda: gaia)
    monkeypatch.setattr(envio, "resolver_borders", lambda g, codigos: {
        c.lower(): {"id": c} for c in codigos
    })
    monkeypatch.setattr(envio, "_nombre_frontera", lambda front: front.nombre_frontera)
    return envio


def test_cada_fila_se_guarda_apenas_quoia_responde(monkeypatch):
    """El bug: si el proceso moría a mitad de lista, no quedaba nada guardado."""
    a, b = _Fila("frta"), _Fila("frtb")

    def _reventar(border_id):
        if border_id == "frtb":
            raise RuntimeError("el proceso murió acá")

    gaia = _Gaia({"frta": {"success": True, "status": "OK"}}, al_consultar=_reventar)
    envio = _preparar(monkeypatch, [a, b], gaia)

    with pytest.raises(RuntimeError):
        envio.estado_quoia_revisar(FECHA)

    assert a.guardada_con == envio.CAMPOS_XM
    assert a.xm_exitoso is True


def test_corta_al_pasar_el_presupuesto_y_dice_cuantas_faltan(monkeypatch):
    filas = [_Fila(f"frt{i}") for i in range(5)]
    reloj = iter([0, 0, 10, 20, 999, 999, 999])  # inicio, y antes de cada fila
    gaia = _Gaia()
    envio = _preparar(monkeypatch, filas, gaia)
    monkeypatch.setattr(envio.time, "monotonic", lambda: next(reloj))

    resp = envio.estado_quoia_revisar(FECHA, presupuesto_s=30)

    assert gaia.consultados == ["frt0", "frt1", "frt2"]
    assert resp["sin_revisar"] == 2
    assert resp["total"] == 5
    assert resp["en_espera"] == 5  # Quoia no respondió nada todavía
    assert [f.guardada_con is not None for f in filas] == [True, True, True, False, False]


def test_primero_las_nunca_miradas_y_despues_las_mas_viejas(monkeypatch):
    ahora = datetime.now(timezone.utc)
    reciente = _Fila("frtreciente", verificado_en=ahora)
    vieja = _Fila("frtvieja", verificado_en=ahora - timedelta(hours=1))
    nueva = _Fila("frtnueva")
    gaia = _Gaia()
    envio = _preparar(monkeypatch, [reciente, vieja, nueva], gaia)

    envio.estado_quoia_revisar(FECHA)

    assert gaia.consultados == ["frtnueva", "frtvieja", "frtreciente"]


def test_no_vuelve_a_consultar_las_ya_resueltas(monkeypatch):
    resuelta, pendiente = _Fila("frtok"), _Fila("frtespera")
    resuelta.xm_exitoso, resuelta.xm_estado = True, "OK"
    gaia = _Gaia({"frtespera": {"success": False, "status": "ERROR"}})
    envio = _preparar(monkeypatch, [resuelta, pendiente], gaia)

    resp = envio.estado_quoia_revisar(FECHA)

    assert gaia.consultados == ["frtespera"]
    assert resuelta.guardada_con is None
    assert (resp["exitoso"], resp["error"], resp["en_espera"], resp["sin_revisar"]) == (1, 1, 0, 0)
    assert [f["nombre_proyecto"] for f in resp["fallidas"]] == ["frtespera"]


def test_el_presupuesto_cabe_en_el_timeout_de_gunicorn():
    """75 s de revisión + 30 s de la última llamada a Quoia < 120 s."""
    from apps.energia.services.reporte import envio

    assert envio._PRESUPUESTO_REVISION_S + 30 < 120
