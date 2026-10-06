"""Catálogo de grupos de servicio (`apps/contratos/services/grupos.py`).

Lo que se prueba acá es la cardinalidad que define el negocio y que el esquema
no sabe expresar: en Operación cada subservicio tiene su contrato, y solo
representación+CGM puede traer dos en uno.

No tocan la base: `subservicios_de` lee `contrato.servicios.all()` y `tarifa_de`
lee atributos, así que un objeto suelto sirve y las pruebas corren sin
`django_db`. Lo que escribe la tabla `servicios` se prueba en
`test_contratos_servicios_registrados.py`.
"""

from decimal import Decimal

import pytest

from apps.contratos.services import grupos


class _Servicios:
    """Imita `contrato.servicios` (la relación con la tabla `servicios`)."""

    def __init__(self, nombres):
        self._filas = [type("Servicio", (), {"servicio": n})() for n in nombres]

    def all(self):
        return self._filas


class ContratoFalso:
    """Lo mínimo que leen las funciones del catálogo. Con `servicios`, es un
    contrato ya guardado (tiene `pk`) con esas filas registradas."""

    def __init__(self, servicio_aplica, tarifa_representacion=None,
                 tarifa_cgm=None, tarifa_base=None, servicios=None):
        self.servicio_aplica = servicio_aplica
        self.tarifa_representacion = tarifa_representacion
        self.tarifa_cgm = tarifa_cgm
        self.tarifa_base = tarifa_base
        self.pk = 1 if servicios is not None else None
        self.servicios = _Servicios(servicios or [])


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

def test_un_contrato_registrado_con_los_dos_cubre_los_dos_subservicios():
    """El caso general que `servicio_aplica` no puede expresar."""
    contrato = ContratoFalso("representacion", servicios=["cgm", "representacion"])
    assert grupos.subservicios_de(contrato) == ["representacion", "cgm"]  # orden del catálogo


def test_la_tarifa_ya_no_decide_que_servicios_cubre():
    """Antes, un contrato con solo tarifa de CGM era 'de CGM'. Ahora manda lo
    registrado: la tarifa puede llegar después, o nunca."""
    contrato = ContratoFalso("representacion", tarifa_cgm=Decimal("1.25"),
                             servicios=["representacion", "cgm"])
    assert grupos.subservicios_de(contrato) == ["representacion", "cgm"]


def test_solo_representacion():
    contrato = ContratoFalso("representacion", servicios=["representacion"])
    assert grupos.subservicios_de(contrato) == ["representacion"]


def test_sin_guardar_cae_a_servicio_aplica():
    """Un contrato todavía sin guardar no puede desaparecer de su pestaña."""
    contrato = ContratoFalso("cgm")
    assert grupos.subservicios_de(contrato) == ["cgm"]


def test_cada_subservicio_tiene_su_propia_tarifa():
    contrato = ContratoFalso(
        "representacion",
        tarifa_representacion=Decimal("0.5"), tarifa_cgm=Decimal("1.25"),
        servicios=["representacion", "cgm"],
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


# ── El filtro de ORM ──────────────────────────────────────────────────────
# Que diga lo mismo que `subservicios_de()` se prueba contra una base, en
# `test_contratos_servicios_registrados.py`.

def test_el_filtro_lee_la_tabla_servicios():
    from apps.contratos.services.grupos import filtro_subservicio

    assert filtro_subservicio("cgm").children == [("servicios__servicio", "cgm")]
