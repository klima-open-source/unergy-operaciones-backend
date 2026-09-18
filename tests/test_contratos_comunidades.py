"""Comunidades energéticas: a esas plantas no se les presta representación ni CGM.

Regla de negocio (2026-09-18): el PPA de comunidad excluye esos dos servicios
desde su fecha de entrada. Lo que se prueba acá es que la exclusión **no escribe
nada** en los contratos --siguen "firmados", solo dejan de aplicar-- y que al
salir de la comunidad vuelven solos.
"""

import os
from datetime import date

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("SECRET_KEY", "x" * 40)
django.setup()

from apps.contratos.services import comunidades  # noqa: E402

HOY = date(2026, 9, 18)


class ContratoFalso:
    def __init__(self, servicio_aplica="representacion", proyecto_id=7,
                 estado="firmado", fecha_fin=None,
                 tarifa_representacion=None, tarifa_cgm=None, tarifa_base=None):
        self.servicio_aplica = servicio_aplica
        self.proyecto_id = proyecto_id
        self.estado = estado
        self.fecha_fin = fecha_fin
        self.tarifa_representacion = tarifa_representacion
        self.tarifa_cgm = tarifa_cgm
        self.tarifa_base = tarifa_base


# ── La exclusión ──────────────────────────────────────────────────────────

def test_una_planta_en_comunidad_no_recibe_representacion():
    contrato = ContratoFalso(tarifa_representacion=1)
    assert comunidades.presta_servicio(contrato, "representacion", HOY, {7}) is False


def test_tampoco_cgm():
    contrato = ContratoFalso(tarifa_cgm=1)
    assert comunidades.presta_servicio(contrato, "cgm", HOY, {7}) is False


def test_pero_si_recibe_los_de_operacion():
    """El O&M se sigue prestando: lo que cambia es quién comercializa la energía."""
    for servicio in ("mantenimiento", "arriendo", "internet"):
        contrato = ContratoFalso(servicio_aplica=servicio, tarifa_base=1)
        assert comunidades.presta_servicio(contrato, servicio, HOY, {7}) is True


def test_una_planta_fuera_de_la_comunidad_recibe_todo():
    contrato = ContratoFalso(tarifa_representacion=1)
    assert comunidades.presta_servicio(contrato, "representacion", HOY, set()) is True


def test_un_contrato_sin_planta_no_se_excluye():
    contrato = ContratoFalso(proyecto_id=None, tarifa_representacion=1)
    assert comunidades.presta_servicio(contrato, "representacion", HOY, {7}) is True


# ── La vigencia sigue mandando ────────────────────────────────────────────

def test_un_contrato_terminado_no_se_presta_aunque_no_haya_comunidad():
    contrato = ContratoFalso(estado="terminado", tarifa_representacion=1)
    assert comunidades.presta_servicio(contrato, "representacion", HOY, set()) is False


def test_un_contrato_vencido_tampoco():
    contrato = ContratoFalso(fecha_fin=date(2025, 1, 1), tarifa_representacion=1)
    assert comunidades.presta_servicio(contrato, "representacion", HOY, set()) is False


# ── Salir de la comunidad: los servicios vuelven solos ────────────────────

def test_al_salir_de_la_comunidad_el_servicio_vuelve():
    """El contrato nunca se tocó, así que basta con que la planta deje la lista.

    Es la razón de calcular la exclusión en vez de escribir `terminado`: si se
    hubiera escrito, al salir no habría forma de saber cuáles cerró el sistema.
    """
    contrato = ContratoFalso(tarifa_representacion=1)
    assert comunidades.presta_servicio(contrato, "representacion", HOY, {7}) is False
    assert comunidades.presta_servicio(contrato, "representacion", HOY, set()) is True
    # Y el contrato sigue intacto.
    assert contrato.estado == "firmado"


def test_los_subservicios_prestados_filtran_los_excluidos():
    """Un contrato que cubre los dos, en comunidad, no presta ninguno."""
    contrato = ContratoFalso(tarifa_representacion=1, tarifa_cgm=2)
    assert comunidades.subservicios_prestados(contrato, HOY, set()) == [
        "representacion", "cgm"
    ]
    assert comunidades.subservicios_prestados(contrato, HOY, {7}) == []


# ── El mensaje que bloquea la creación ────────────────────────────────────

def test_bloquea_crear_representacion_en_una_planta_en_comunidad():
    motivo = comunidades.motivo_bloqueo(7, "representacion", {7})
    assert motivo is not None
    assert "comunidad energética" in motivo


def test_no_bloquea_mantenimiento():
    assert comunidades.motivo_bloqueo(7, "mantenimiento", {7}) is None


def test_no_bloquea_si_la_planta_no_esta_en_comunidad():
    assert comunidades.motivo_bloqueo(7, "representacion", set()) is None


def test_no_bloquea_un_contrato_sin_planta():
    assert comunidades.motivo_bloqueo(None, "representacion", {7}) is None


# ── Lo que la consulta tiene que mirar ────────────────────────────────────

def test_la_consulta_exige_ppa_vivo_y_fecha_cumplida():
    """Un PPA de comunidad vencido, o cuya fecha aún no llega, no excluye nada.

    Se comprueba sobre el SQL porque la consulta es la parte que no se puede
    probar sin base: si alguien le quita una de las dos condiciones, una planta
    perdería sus servicios antes de tiempo o los perdería para siempre.
    """
    from django.db.models import Q

    from apps.contratos.services import vigencia

    sql = str(vigencia.filtro_ppa_vivos(HOY))
    assert "fecha_fin" in sql
    assert isinstance(vigencia.filtro_ppa_vivos(HOY), Q)
    # La otra mitad vive en `plantas_en_comunidad`, que filtra por
    # `es_comunidad_energetica` y por `fecha_entrada_comunidad`.
    import inspect
    fuente = inspect.getsource(comunidades.plantas_en_comunidad)
    assert "es_comunidad_energetica=True" in fuente
    assert "fecha_entrada_comunidad__lte" in fuente
    assert "filtro_ppa_vivos" in fuente


def test_el_campo_existe_en_el_modelo():
    from apps.ppa.models import PpaContrato

    campos = {f.name for f in PpaContrato._meta.get_fields()}
    assert "fecha_entrada_comunidad" in campos
    assert "es_comunidad_energetica" in campos
