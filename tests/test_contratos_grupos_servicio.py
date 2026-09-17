"""Catálogo de grupos de servicio (`apps/contratos/services/grupos.py`).

Lo que se prueba acá es la cardinalidad que define el negocio y que el esquema
no sabe expresar: en Operación cada subservicio tiene su contrato, y solo
representación+CGM puede traer dos en uno.

No tocan la base: `subservicios_de` y `tarifa_de` leen atributos, así que un
objeto suelto sirve y las pruebas corren sin `django_db`.
"""

from decimal import Decimal

import pytest

from apps.contratos.services import grupos


class ContratoFalso:
    """Lo mínimo que leen las funciones del catálogo."""

    def __init__(self, servicio_aplica, tarifa_representacion=None,
                 tarifa_cgm=None, tarifa_base=None):
        self.servicio_aplica = servicio_aplica
        self.tarifa_representacion = tarifa_representacion
        self.tarifa_cgm = tarifa_cgm
        self.tarifa_base = tarifa_base


class PpaFalso:
    def __init__(self, tipo_contrato):
        self.tipo_contrato = tipo_contrato


# ── Estructura del catálogo ───────────────────────────────────────────────

def test_los_tres_grupos_y_su_orden():
    assert grupos.ORDEN_GRUPOS == ("ppa", "representacion_cgm", "operacion")


def test_cada_subservicio_pertenece_a_un_solo_grupo():
    todos = [s for subs in grupos.SUBSERVICIOS.values() for s in subs]
    assert len(todos) == len(set(todos))


def test_el_mapa_inverso_cubre_todos_los_subservicios():
    for grupo, subs in grupos.SUBSERVICIOS.items():
        for sub in subs:
            assert grupos.grupo_de(sub) == grupo


def test_todo_subservicio_de_contrato_servicio_tiene_columna_de_tarifa():
    for sub in grupos.SUBSERVICIOS_DE_CONTRATO_SERVICIO:
        assert sub in grupos.COLUMNA_TARIFA


def test_compra_y_venta_no_viven_en_contratos_servicio():
    # Salen de `ppa_contratos.tipo_contrato`, no de `servicio_aplica`.
    assert "compra" not in grupos.SUBSERVICIOS_DE_CONTRATO_SERVICIO
    assert "venta" not in grupos.SUBSERVICIOS_DE_CONTRATO_SERVICIO


def test_un_subservicio_desconocido_no_tiene_grupo():
    assert grupos.grupo_de("promotor") is None
    assert grupos.grupo_de(None) is None


# ── Operación: un contrato por subservicio, siempre uno ───────────────────

@pytest.mark.parametrize("aplica", ["mantenimiento", "arriendo", "internet"])
def test_operacion_siempre_trae_un_solo_subservicio(aplica):
    contrato = ContratoFalso(aplica, tarifa_base=Decimal("100"))
    assert grupos.subservicios_de(contrato) == [aplica]
    assert grupos.grupo_de_contrato(contrato) == "operacion"


def test_operacion_ignora_las_tarifas_de_representacion_y_cgm():
    """Un contrato de arriendo con `tarifa_cgm` sucia sigue siendo solo arriendo.

    La derivación por tarifas aplica únicamente a `representacion_cgm`; si se
    aplicara a todo, un dato basura movería el contrato de grupo.
    """
    contrato = ContratoFalso(
        "arriendo", tarifa_cgm=Decimal("7"), tarifa_base=Decimal("100")
    )
    assert grupos.subservicios_de(contrato) == ["arriendo"]


def test_los_tres_de_operacion_comparten_tarifa_base():
    contrato = ContratoFalso("internet", tarifa_base=Decimal("55.5"))
    assert grupos.tarifa_de(contrato, "internet") == Decimal("55.5")


# ── Representación y CGM: el único caso de dos en uno ─────────────────────

def test_un_contrato_con_las_dos_tarifas_cubre_los_dos_subservicios():
    """El caso general que `servicio_aplica` no puede expresar."""
    contrato = ContratoFalso(
        "representacion",
        tarifa_representacion=Decimal("0.5"), tarifa_cgm=Decimal("1.25"),
    )
    assert grupos.subservicios_de(contrato) == ["representacion", "cgm"]


def test_grabado_como_representacion_pero_solo_con_tarifa_cgm_es_cgm():
    """Lo que hoy queda invisible: la etiqueta dice una cosa y el dato otra."""
    contrato = ContratoFalso("representacion", tarifa_cgm=Decimal("1.25"))
    assert grupos.subservicios_de(contrato) == ["cgm"]


def test_solo_representacion():
    contrato = ContratoFalso(
        "representacion", tarifa_representacion=Decimal("0.5")
    )
    assert grupos.subservicios_de(contrato) == ["representacion"]


def test_sin_tarifas_cae_a_servicio_aplica():
    """Un contrato recién creado no puede desaparecer de su pestaña."""
    contrato = ContratoFalso("cgm")
    assert grupos.subservicios_de(contrato) == ["cgm"]


def test_tarifa_en_cero_cuenta_como_subservicio_contratado():
    """Cero es un valor cargado, no un dato ausente. Solo `None` significa 'no'."""
    contrato = ContratoFalso(
        "representacion",
        tarifa_representacion=Decimal("0.5"), tarifa_cgm=Decimal("0"),
    )
    assert grupos.subservicios_de(contrato) == ["representacion", "cgm"]


def test_cada_subservicio_tiene_su_propia_tarifa():
    contrato = ContratoFalso(
        "representacion",
        tarifa_representacion=Decimal("0.5"), tarifa_cgm=Decimal("1.25"),
    )
    assert grupos.tarifa_de(contrato, "representacion") == Decimal("0.5")
    assert grupos.tarifa_de(contrato, "cgm") == Decimal("1.25")
    assert grupos.tarifas_de(contrato) == {
        "representacion": Decimal("0.5"), "cgm": Decimal("1.25"),
    }


def test_un_valor_fuera_del_catalogo_no_devuelve_subservicios():
    # `promotor` y `rec` existieron en el enum y ya no.
    assert grupos.subservicios_de(ContratoFalso("promotor")) == []


# ── PPA ───────────────────────────────────────────────────────────────────

def test_ppa_compra_y_venta():
    assert grupos.subservicio_de_ppa(PpaFalso("compra")) == "compra"
    assert grupos.subservicio_de_ppa(PpaFalso("venta")) == "venta"


def test_ppa_sin_tipo_se_trata_como_venta():
    """`tipo_contrato` admite nulo y su default es `venta`."""
    assert grupos.subservicio_de_ppa(PpaFalso(None)) == "venta"


# ── El catálogo que consume el front ──────────────────────────────────────

def test_catalogo_expone_los_grupos_en_orden():
    assert [g["grupo"] for g in grupos.catalogo()] == list(grupos.ORDEN_GRUPOS)


def test_catalogo_expone_los_subservicios_de_cada_grupo():
    por_grupo = {g["grupo"]: g["subservicios"] for g in grupos.catalogo()}
    assert por_grupo["operacion"] == ["mantenimiento", "arriendo", "internet"]
    assert por_grupo["representacion_cgm"] == ["representacion", "cgm"]
    assert por_grupo["ppa"] == ["compra", "venta"]
