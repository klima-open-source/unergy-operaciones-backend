"""Cada inversionista paga SU tarifa de representación, no la de otro.

En las minigranjas hay un contrato de representación por inversionista y sus
tarifas pueden diferir (negocio, 2026-09-18). Antes se tomaba UN contrato --el
que `elegir_contrato_representacion` considerara mejor-- se aplicaba su tarifa a
toda la energía y el resultado se repartía por participación: un inversionista
con tarifa 7 pagaba como si tuviera 3.

Lo que se prueba acá es la aritmética, sin base: las funciones reciben los
contratos y los inversionistas ya resueltos.
"""

import os
from decimal import Decimal

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("SECRET_KEY", "x" * 40)
django.setup()

from apps.contabilidad.services import costos  # noqa: E402
from apps.contabilidad.services.panel import _valor_linea  # noqa: E402


class ContratoFalso:
    def __init__(self, inversionista, rep=None, cgm=None, admin=None):
        self.inversionista_nombre = inversionista
        # El modelo real siempre tiene la columna, aunque venga en nulo:
        # los contratos del wizard no la pueblan.
        self.inversionista_id = None
        self.tarifa_representacion = Decimal(str(rep)) if rep is not None else None
        self.tarifa_cgm = Decimal(str(cgm)) if cgm is not None else None
        self.tarifa_admin = Decimal(str(admin)) if admin is not None else None
        self.indexacion_representacion = None
        self.indexacion_cgm = None
        self.fecha_firma_contrato = None
        self.estado = "firmado"
        self.fecha_fin = None
        self.id = id(self)


def _inv(id_, nombre, fraccion):
    return {"id": id_, "nombre": nombre, "fraccion": fraccion, "pct": fraccion * 100}


# ── El emparejamiento contrato ↔ inversionista ────────────────────────────

def test_empareja_por_nombre_cuando_no_hay_clave_foranea():
    """El respaldo: contratos sin `inversionista_id`, como los del wizard."""
    contratos = [ContratoFalso("Quantum S.A.S.", rep=3), ContratoFalso("BETA LTDA", rep=5)]
    invs = [_inv(1, "QUANTUM SAS", 0.5), _inv(2, "Beta Ltda.", 0.5)]

    por_inv = costos.contratos_por_inversionista(contratos, invs)
    assert por_inv[1].inversionista_nombre == "Quantum S.A.S."
    assert por_inv[2].inversionista_nombre == "BETA LTDA"


def test_con_un_solo_contrato_no_hay_desglose():
    """En GD hay un único contrato: nada cambia respecto al cálculo anterior."""
    contratos = [ContratoFalso("Quantum", rep=3)]
    assert costos.contratos_por_inversionista(contratos, [_inv(1, "Quantum", 1.0)]) == {}


def test_sin_inversionistas_no_hay_desglose():
    contratos = [ContratoFalso("A", rep=3), ContratoFalso("B", rep=5)]
    assert costos.contratos_por_inversionista(contratos, None) == {}
    assert costos.contratos_por_inversionista(contratos, []) == {}


def test_si_los_nombres_no_emparejan_se_conserva_el_calculo_anterior():
    """Ante datos que no cuadran, el comportamiento de siempre."""
    contratos = [ContratoFalso("Quantum", rep=3), ContratoFalso("Beta", rep=5)]
    invs = [_inv(1, "Otra Cosa", 0.5), _inv(2, "Distinto", 0.5)]
    assert costos.contratos_por_inversionista(contratos, invs) == {}


def test_un_contrato_sin_nombre_no_empareja_con_nadie():
    """`inversionista_nombre` vacío es el caso del wizard: no se le asigna a nadie."""
    contratos = [ContratoFalso("", rep=3), ContratoFalso("Beta", rep=5)]
    invs = [_inv(1, "", 0.5), _inv(2, "Beta", 0.5)]
    por_inv = costos.contratos_por_inversionista(contratos, invs)
    assert 1 not in por_inv
    assert por_inv[2].tarifa_representacion == Decimal("5")


# ── La aritmética ─────────────────────────────────────────────────────────

def test_cada_uno_paga_su_tarifa_por_su_parte_de_la_energia():
    """3/5/7 $/kWh con 50/30/20 sobre 1.000 kWh."""
    contratos = [
        ContratoFalso("A", rep=3), ContratoFalso("B", rep=5), ContratoFalso("C", rep=7),
    ]
    invs = [_inv(1, "A", 0.5), _inv(2, "B", 0.3), _inv(3, "C", 0.2)]
    por_inv = costos.contratos_por_inversionista(contratos, invs)
    fracciones = costos._fracciones(invs)

    out = {}
    costos._agregar(out, "Representación", "servicios", contratos[0], por_inv,
                    fracciones, "2026-09", 1000.0,
                    lambda x: (x.indexacion_representacion, x.tarifa_representacion))

    detalle = out["Representación"]["valor_por_inversionista"]
    assert detalle[1] == -1500.0    # 3 × 1000 × 0,5
    assert detalle[2] == -1500.0    # 5 × 1000 × 0,3
    assert detalle[3] == -1400.0    # 7 × 1000 × 0,2
    # El total de la planta es la SUMA, no una tarifa aplicada a todo.
    assert out["Representación"]["valor"] == -4400.0


def test_lo_que_daba_antes_y_por_que_estaba_mal():
    """Con el cálculo viejo, la misma planta daba -3.000 y todos pagaban 3 $/kWh.

    `elegir_contrato_representacion` habría tomado uno --da igual cuál para este
    ejemplo-- y el panel repartiría ese total por participación: 1.500 / 900 /
    600. B y C pagaban de menos y el total de la planta quedaba corto en 1.400.
    """
    viejo_total = -abs(3 * 1000)              # una sola tarifa sobre toda la energía
    assert viejo_total == -3000
    assert [round(viejo_total * f, 2) for f in (0.5, 0.3, 0.2)] == [-1500.0, -900.0, -600.0]
    # Lo correcto (ver la prueba anterior): -1500 / -1500 / -1400, total -4400.


def test_si_todos_tienen_la_misma_tarifa_el_total_no_cambia():
    """Es el caso en que el cálculo viejo ya acertaba."""
    contratos = [ContratoFalso("A", rep=4), ContratoFalso("B", rep=4)]
    invs = [_inv(1, "A", 0.6), _inv(2, "B", 0.4)]
    out = {}
    costos._agregar(out, "Representación", "servicios", contratos[0],
                    costos.contratos_por_inversionista(contratos, invs),
                    costos._fracciones(invs), "2026-09", 1000.0,
                    lambda x: (x.indexacion_representacion, x.tarifa_representacion))
    assert out["Representación"]["valor"] == -4000.0    # 4 × 1000, como antes


def test_un_inversionista_sin_tarifa_no_aporta():
    contratos = [ContratoFalso("A", rep=3), ContratoFalso("B", rep=None)]
    invs = [_inv(1, "A", 0.5), _inv(2, "B", 0.5)]
    out = {}
    costos._agregar(out, "Representación", "servicios", contratos[0],
                    costos.contratos_por_inversionista(contratos, invs),
                    costos._fracciones(invs), "2026-09", 1000.0,
                    lambda x: (x.indexacion_representacion, x.tarifa_representacion))
    assert out["Representación"]["valor_por_inversionista"] == {1: -1500.0}
    assert out["Representación"]["valor"] == -1500.0


def test_administracion_tambien_va_por_contrato():
    """Es un porcentaje sobre el ingreso, y también vive en el contrato."""
    contratos = [ContratoFalso("A", admin=0.02), ContratoFalso("B", admin=0.05)]
    invs = [_inv(1, "A", 0.5), _inv(2, "B", 0.5)]
    out = {}
    costos._agregar_admin(out, contratos[0],
                          costos.contratos_por_inversionista(contratos, invs),
                          costos._fracciones(invs), 1_000_000)
    detalle = out["Administración"]["valor_por_inversionista"]
    assert detalle[1] == -10000.0   # 2% × 1.000.000 × 0,5
    assert detalle[2] == -25000.0   # 5% × 1.000.000 × 0,5
    assert out["Administración"]["valor"] == -35000.0


# ── El reparto del Panel ──────────────────────────────────────────────────

def test_el_panel_usa_el_valor_propio_cuando_existe():
    linea = {"valor": -4400.0, "valor_por_inversionista": {1: -1500.0, 2: -1500.0, 3: -1400.0}}
    assert _valor_linea(linea, 1, 0.5) == -1500.0
    assert _valor_linea(linea, 3, 0.2) == -1400.0


def test_el_panel_reparte_por_fraccion_los_demas_conceptos():
    """Lo que no sale de un contrato por inversionista sigue igual."""
    linea = {"valor": -1000.0}
    assert _valor_linea(linea, 1, 0.5) == -500.0
    assert _valor_linea(linea, 2, 0.3) == -300.0


def test_un_inversionista_fuera_del_desglose_cae_a_la_fraccion():
    """No se le deja en cero: recibe el trato de antes."""
    linea = {"valor": -1000.0, "valor_por_inversionista": {1: -600.0}}
    assert _valor_linea(linea, 1, 0.6) == -600.0
    assert _valor_linea(linea, 99, 0.4) == -400.0


# ── El emparejamiento por clave foránea ───────────────────────────────────

class ContratoConFK(ContratoFalso):
    """Un contrato con `inversionista_id` poblado: el cruce exacto."""

    def __init__(self, inversionista, cliente_id, **kw):
        super().__init__(inversionista, **kw)
        self.inversionista_id = cliente_id


def _inv_con_cliente(id_, cliente_id, nombre, fraccion):
    return {"id": id_, "cliente_id": cliente_id, "nombre": nombre,
            "fraccion": fraccion, "pct": fraccion * 100}


def test_empareja_por_cliente_aunque_el_nombre_no_coincida():
    """El FK manda: `inversionista_id` y `cliente_id` apuntan los dos a clientes.

    Es lo que hace el cruce exacto. El nombre del contrato puede estar escrito
    distinto --o vacío-- y el emparejamiento sigue siendo correcto.
    """
    contratos = [
        ContratoConFK("escrito de otra forma", cliente_id=10, rep=3),
        ContratoConFK("", cliente_id=20, rep=7),
    ]
    invs = [_inv_con_cliente(1, 10, "Quantum S.A.S.", 0.5),
            _inv_con_cliente(2, 20, "Beta Ltda.", 0.5)]

    por_inv = costos.contratos_por_inversionista(contratos, invs)
    assert por_inv[1].tarifa_representacion == Decimal("3")
    assert por_inv[2].tarifa_representacion == Decimal("7")


def test_cae_al_nombre_cuando_el_contrato_no_tiene_el_FK():
    """Los contratos del wizard no guardan `inversionista_id`: el campo es texto."""
    contratos = [
        ContratoConFK("Quantum SAS", cliente_id=None, rep=3),
        ContratoConFK("Beta", cliente_id=None, rep=7),
    ]
    invs = [_inv_con_cliente(1, 10, "Quantum S.A.S.", 0.5),
            _inv_con_cliente(2, 20, "Beta", 0.5)]

    por_inv = costos.contratos_por_inversionista(contratos, invs)
    assert por_inv[1].tarifa_representacion == Decimal("3")
    assert por_inv[2].tarifa_representacion == Decimal("7")


def test_mezcla_los_dos_caminos():
    """Uno con FK y otro sin él: cada uno empareja por donde puede."""
    contratos = [
        ContratoConFK("da igual", cliente_id=10, rep=3),
        ContratoConFK("Beta", cliente_id=None, rep=7),
    ]
    invs = [_inv_con_cliente(1, 10, "Quantum", 0.5),
            _inv_con_cliente(2, 20, "Beta", 0.5)]

    por_inv = costos.contratos_por_inversionista(contratos, invs)
    assert por_inv[1].tarifa_representacion == Decimal("3")
    assert por_inv[2].tarifa_representacion == Decimal("7")


def test_el_FK_gana_sobre_un_nombre_que_apunta_a_otro():
    """Si los dos datos se contradicen, manda el que no es texto libre."""
    contratos = [
        ContratoConFK("Beta", cliente_id=10, rep=3),   # nombre de B, cliente de A
        ContratoConFK("Quantum", cliente_id=20, rep=7),
    ]
    invs = [_inv_con_cliente(1, 10, "Quantum", 0.5),
            _inv_con_cliente(2, 20, "Beta", 0.5)]

    por_inv = costos.contratos_por_inversionista(contratos, invs)
    assert por_inv[1].tarifa_representacion == Decimal("3")   # por cliente_id 10
    assert por_inv[2].tarifa_representacion == Decimal("7")   # por cliente_id 20
