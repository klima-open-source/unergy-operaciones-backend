"""La comparación de fotos antes/después del corte de contratos (deploy 2).

Lo que se prueba es la parte pura: que dos fotos "iguales" —con los ids de servicio
cambiados según la correspondencia— no den diferencias, y que cada cosa que el corte
pudiera romper sí aparezca.
"""
import copy
import os

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("SECRET_KEY", "x" * 40)
django.setup()

from apps.contratos.management.commands import verificar_corte_contratos as v  # noqa: E402


def _fotos():
    antes = {
        "momento": "antes",
        "fks": [
            {"tabla": "ppa_tarifas", "columna": "contrato_id", "apunta_a": "ppa_contratos",
             "nombre": "ppa_tarifas_contrato_id_fkey",
             "definicion": "FOREIGN KEY (contrato_id) REFERENCES ppa_contratos(id) "
                           "ON DELETE CASCADE"},
            {"tabla": "contrato_fronteras", "columna": "contrato_servicio_id",
             "apunta_a": "contratos_servicio", "nombre": "cf_fk",
             "definicion": "FOREIGN KEY (contrato_servicio_id) "
                           "REFERENCES contratos_servicio(id)"},
        ],
        "valores": {
            "ppa_tarifas.contrato_id": {"1": 3, "2": 1},
            "contrato_fronteras.contrato_servicio_id": {"1": 2, "5": 1},
        },
        "correspondencia": {"1": 101, "5": 105},
        "n_ppa": 2, "n_servicio": 2,
        "subservicios": {"1": ["cgm", "representacion"], "5": ["mantenimiento"]},
        "subservicio_ppa": {"1": "venta", "2": "compra"},
        "plantas_por_ppa": {"1": [7, 8], "2": [9]},
        "facturacion": {"2026-08": {"contratos": 4, "facturacion_total": 10.5}},
    }
    despues = {
        "momento": "despues",
        "fks": [
            {"tabla": "ppa_tarifas", "columna": "contrato_id", "apunta_a": "contratos",
             "nombre": "ppa_tarifas_contrato_id_fkey",
             "definicion": "FOREIGN KEY (contrato_id) REFERENCES contratos(id) "
                           "ON DELETE CASCADE"},
            {"tabla": "contrato_fronteras", "columna": "contrato_servicio_id",
             "apunta_a": "contratos", "nombre": "cf_fk",
             "definicion": "FOREIGN KEY (contrato_servicio_id) REFERENCES contratos(id)"},
        ],
        "valores": {
            "ppa_tarifas.contrato_id": {"1": 3, "2": 1},
            "contrato_fronteras.contrato_servicio_id": {"101": 2, "105": 1},
        },
        "correspondencia": {"1": 101, "5": 105},
        "n_ppa": 2, "n_servicio": 2,
        "subservicios": {"101": ["cgm", "representacion"], "105": ["mantenimiento"]},
        "subservicio_ppa": {"1": "venta", "2": "compra"},
        "plantas_por_ppa": {"1": [7, 8], "2": [9]},
        "facturacion": {"2026-08": {"contratos": 4, "facturacion_total": 10.5}},
    }
    return antes, despues


def test_un_corte_correcto_no_tiene_diferencias():
    assert v.comparar(*_fotos()) == []


def test_la_definicion_se_compara_como_si_apuntara_a_contratos():
    assert v.normalizar_definicion(
        "FOREIGN KEY (x) REFERENCES public.contratos_servicio(id) ON DELETE SET NULL"
    ) == "FOREIGN KEY (x) REFERENCES contratos(id) ON DELETE SET NULL"


def test_una_fila_que_quedo_apuntando_a_otro_contrato():
    antes, despues = _fotos()
    despues["valores"]["contrato_fronteras.contrato_servicio_id"] = {"101": 1, "105": 2}
    [d] = v.comparar(antes, despues)
    assert "contrato_fronteras.contrato_servicio_id" in d and "cambiaron sus valores" in d


def test_una_llave_que_se_perdio_o_cambio_su_on_delete():
    antes, despues = _fotos()
    perdida = copy.deepcopy(despues)
    perdida["fks"].pop(0)
    assert any("ya no está" in d for d in v.comparar(antes, perdida))

    cambiada = copy.deepcopy(despues)
    cambiada["fks"][0]["definicion"] = cambiada["fks"][0]["definicion"].replace(
        "CASCADE", "SET NULL")
    assert any("cambió la definición" in d for d in v.comparar(antes, cambiada))


def test_una_llave_que_sigue_apuntando_a_la_tabla_vieja():
    antes, despues = _fotos()
    despues["fks"][1]["apunta_a"] = "contratos_servicio"
    assert any("no a contratos" in d for d in v.comparar(antes, despues))


def test_lo_que_ve_la_app_y_la_facturacion():
    antes, despues = _fotos()
    despues["subservicios"]["101"] = ["representacion"]
    despues["plantas_por_ppa"]["2"] = []
    despues["facturacion"]["2026-08"]["facturacion_total"] = 9.0
    diferencias = v.comparar(antes, despues)
    assert len(diferencias) == 3
    assert any(d.startswith("servicios del contrato de servicio 101") for d in diferencias)
    assert any(d.startswith("plantas del PPA 2") for d in diferencias)
    assert any("Facturación 2026-08" in d and "10.5" in d for d in diferencias)


def test_sin_correspondencia_no_se_pueden_traducir_los_ids_de_servicio():
    antes, despues = _fotos()
    del antes["correspondencia"], despues["correspondencia"]
    assert any("no hay correspondencia" in d for d in v.comparar(antes, despues))


def test_fotos_en_el_orden_equivocado():
    antes, despues = _fotos()
    assert v.comparar(despues, antes)[0].startswith("Las fotos no son antes/después")
