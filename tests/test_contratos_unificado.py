"""Forma común de servicio (`apps/contratos/services/unificado.py`).

Lo que se prueba es que un contrato de servicio y un PPA salgan comparables sin
perder lo que los distingue: las partes conservan sus nombres propios, y contar
contratos no es lo mismo que contar plantas.

Sin base: las funciones leen atributos y reciben proyectos y enlaces ya
resueltos.
"""

import os
from datetime import date
from decimal import Decimal

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


# Django se arranca a nivel de modulo, NO en un fixture: `unificado` reusa
# `semaforo_contrato` de `apps.clientes.services.panel`, que importa modelos al
# cargarse, y ese import ocurre durante la coleccion -- antes de que corra
# cualquier fixture. No se consulta la base: las pruebas pasan objetos sueltos.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("SECRET_KEY", "x" * 40)
django.setup()

from apps.contratos.services import unificado  # noqa: E402 -- tras django.setup()

HOY = date(2026, 9, 16)


class ProyectoFalso:
    def __init__(self, id, nombre_comercial="Planta", tipo_proyecto="gd"):
        self.id = id
        self.nombre_comercial = nombre_comercial
        self.tipo_proyecto = tipo_proyecto


class ContratoFalso:
    def __init__(self, servicio_aplica, **kwargs):
        self.id = kwargs.get("id", 1)
        self.servicio_aplica = servicio_aplica
        self.numero_contrato = kwargs.get("numero_contrato")
        self.fecha_inicio = kwargs.get("fecha_inicio")
        self.fecha_fin = kwargs.get("fecha_fin")
        self.estado = kwargs.get("estado", "vigente")
        self.renovacion_automatica = kwargs.get("renovacion_automatica")
        self.contratante_nombre = kwargs.get("contratante_nombre")
        self.prestador_nombre = kwargs.get("prestador_nombre")
        self.tarifa_representacion = kwargs.get("tarifa_representacion")
        self.tarifa_cgm = kwargs.get("tarifa_cgm")
        self.tarifa_base = kwargs.get("tarifa_base")


class PpaFalso:
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 100)
        self.tipo_contrato = kwargs.get("tipo_contrato", "venta")
        self.numero_codigo_contrato = kwargs.get("numero_codigo_contrato")
        self.nombre_interno = kwargs.get("nombre_interno")
        self.fecha_inicio = kwargs.get("fecha_inicio")
        self.fecha_fin = kwargs.get("fecha_fin")
        self.renovacion_automatica = kwargs.get("renovacion_automatica")
        self.comprador_nombre = kwargs.get("comprador_nombre")
        self.vendedor_nombre = kwargs.get("vendedor_nombre")


# ── Contrato de servicio ──────────────────────────────────────────────────

def test_contrato_de_operacion_sale_en_su_grupo():
    contrato = ContratoFalso("mantenimiento", tarifa_base=Decimal("100"))
    salida = unificado.desde_contrato_servicio(contrato, HOY)
    assert salida["grupo"] == "operacion"
    assert salida["subservicios"] == ["mantenimiento"]
    assert salida["fuente"] == "servicio"


def test_contrato_de_representacion_y_cgm_trae_los_dos_subservicios():
    contrato = ContratoFalso(
        "representacion",
        tarifa_representacion=Decimal("0.5"), tarifa_cgm=Decimal("1.25"),
    )
    salida = unificado.desde_contrato_servicio(contrato, HOY)
    assert salida["grupo"] == "representacion_cgm"
    assert salida["subservicios"] == ["representacion", "cgm"]
    assert salida["tarifas"] == {
        "representacion": Decimal("0.5"), "cgm": Decimal("1.25"),
    }


def test_las_partes_de_un_contrato_de_servicio_son_contratante_y_prestador():
    contrato = ContratoFalso(
        "arriendo", contratante_nombre="Unergy", prestador_nombre="Don José",
    )
    partes = unificado.desde_contrato_servicio(contrato, HOY)["partes"]
    assert partes == {"contratante": "Unergy", "prestador": "Don José"}
    # No se renombran a una "contraparte" generica.
    assert "comprador" not in partes


def test_un_contrato_de_servicio_cubre_una_sola_planta():
    contrato = ContratoFalso("internet")
    salida = unificado.desde_contrato_servicio(
        contrato, HOY, proyecto=ProyectoFalso(7, "Villanueva")
    )
    assert salida["plantas"] == [
        {"proyecto_id": 7, "nombre": "Villanueva", "tipo_proyecto": "gd"}
    ]


def test_contrato_terminado_sale_vencido_aunque_la_fecha_no_haya_pasado():
    contrato = ContratoFalso(
        "mantenimiento", estado="terminado", fecha_fin=date(2030, 1, 1)
    )
    assert unificado.desde_contrato_servicio(contrato, HOY)["semaforo"] == "vencido"


def test_semaforo_por_vencer_dentro_del_umbral():
    contrato = ContratoFalso("mantenimiento", fecha_fin=date(2026, 10, 1))
    assert unificado.desde_contrato_servicio(contrato, HOY)["semaforo"] == "por_vencer"


def test_sin_fecha_fin_es_vigente():
    contrato = ContratoFalso("mantenimiento")
    assert unificado.desde_contrato_servicio(contrato, HOY)["semaforo"] == "vigente"


def test_las_fechas_salen_como_texto_iso():
    contrato = ContratoFalso(
        "mantenimiento", fecha_inicio=date(2025, 1, 15), fecha_fin=date(2030, 1, 1)
    )
    salida = unificado.desde_contrato_servicio(contrato, HOY)
    assert salida["fecha_inicio"] == "2025-01-15"
    assert salida["fecha_fin"] == "2030-01-01"


# ── PPA ───────────────────────────────────────────────────────────────────

def test_ppa_sale_en_su_grupo_con_compra_o_venta():
    salida = unificado.desde_ppa(PpaFalso(tipo_contrato="compra"), HOY)
    assert salida["grupo"] == "ppa"
    assert salida["subservicios"] == ["compra"]
    assert salida["fuente"] == "ppa"


def test_las_partes_de_un_ppa_son_comprador_y_vendedor():
    ppa = PpaFalso(comprador_nombre="Cliente A", vendedor_nombre="Unergy")
    partes = unificado.desde_ppa(ppa, HOY)["partes"]
    assert partes == {"comprador": "Cliente A", "vendedor": "Unergy"}
    assert "contratante" not in partes


def test_un_ppa_puede_cubrir_varias_plantas():
    plantas = [ProyectoFalso(1, "A"), ProyectoFalso(2, "B"), ProyectoFalso(3, "C")]
    salida = unificado.desde_ppa(PpaFalso(), HOY, proyectos=plantas)
    assert [p["proyecto_id"] for p in salida["plantas"]] == [1, 2, 3]


def test_el_ppa_no_tiene_estado_porque_la_tabla_no_lo_guarda():
    assert unificado.desde_ppa(PpaFalso(), HOY)["estado"] is None


def test_el_ppa_no_trae_tarifas_porque_cuelgan_del_contrato_por_mes():
    assert unificado.desde_ppa(PpaFalso(), HOY)["tarifas"] == {}


def test_el_numero_del_ppa_cae_al_nombre_interno():
    ppa = PpaFalso(numero_codigo_contrato=None, nombre_interno="PPA Quantum")
    assert unificado.desde_ppa(ppa, HOY)["numero"] == "PPA Quantum"


# ── Conteos: contratos y plantas no son la misma cifra ────────────────────

def test_un_ppa_de_cinco_plantas_es_un_contrato_y_cinco_plantas():
    plantas = [ProyectoFalso(i) for i in range(1, 6)]
    servicios = [unificado.desde_ppa(PpaFalso(), HOY, proyectos=plantas)]
    assert unificado.contar(servicios) == {"contratos": 1, "plantas": 5}


def test_tres_contratos_de_operacion_sobre_una_planta_son_una_sola_planta():
    """Mantenimiento, arriendo e internet de la misma planta: 3 contratos, 1 planta."""
    planta = ProyectoFalso(7, "Villanueva")
    servicios = [
        unificado.desde_contrato_servicio(
            ContratoFalso(tipo, id=i), HOY, proyecto=planta
        )
        for i, tipo in enumerate(["mantenimiento", "arriendo", "internet"], 1)
    ]
    assert unificado.contar(servicios) == {"contratos": 3, "plantas": 1}


def test_un_contrato_sin_planta_no_suma_plantas():
    servicios = [unificado.desde_contrato_servicio(ContratoFalso("arriendo"), HOY)]
    assert unificado.contar(servicios) == {"contratos": 1, "plantas": 0}


def test_las_plantas_se_deduplican_entre_un_ppa_y_un_contrato_de_servicio():
    """La misma planta cubierta por un PPA y por su O&M cuenta una sola vez."""
    planta = ProyectoFalso(7, "Villanueva")
    servicios = [
        unificado.desde_ppa(PpaFalso(), HOY, proyectos=[planta]),
        unificado.desde_contrato_servicio(
            ContratoFalso("mantenimiento"), HOY, proyecto=planta
        ),
    ]
    assert unificado.contar(servicios) == {"contratos": 2, "plantas": 1}


def test_contar_sin_servicios():
    assert unificado.contar([]) == {"contratos": 0, "plantas": 0}


# ── Las dos formas son comparables ────────────────────────────────────────

def test_ppa_y_contrato_de_servicio_exponen_las_mismas_claves():
    """Es lo que permite mostrarlos en una misma tabla."""
    servicio = unificado.desde_contrato_servicio(ContratoFalso("arriendo"), HOY)
    ppa = unificado.desde_ppa(PpaFalso(), HOY)
    assert servicio.keys() == ppa.keys()
