"""Vigencia calculada de un contrato (`apps/contratos/services/vigencia.py`).

Lo que se prueba es la mitad que faltaba: un contrato con `estado='vigente'`
pero `fecha_fin` ya pasada NO está vivo. Los filtros viejos --que solo miraban
el estado-- lo daban por vivo, y el 2026-09-17 eso eran 8 contratos de
representación recibiendo alertas de aniversario que el código decía querer
evitar.
"""

import os
from datetime import date

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("SECRET_KEY", "x" * 40)
django.setup()

from apps.contratos.services import vigencia  # noqa: E402

HOY = date(2026, 9, 17)


class ContratoFalso:
    def __init__(self, estado="vigente", fecha_fin=None):
        self.estado = estado
        self.fecha_fin = fecha_fin


# ── La decisión humana manda sobre la fecha ───────────────────────────────

def test_terminado_es_terminado_aunque_la_fecha_no_haya_llegado():
    contrato = ContratoFalso(estado="terminado", fecha_fin=date(2030, 1, 1))
    assert vigencia.de_contrato(contrato, HOY) == "terminado"
    assert vigencia.esta_vivo(contrato, HOY) is False


def test_terminado_sin_fecha_tambien_es_terminado():
    assert vigencia.de_contrato(ContratoFalso(estado="terminado"), HOY) == "terminado"


def test_en_renovacion_cuenta_como_vivo():
    """Es justo cuando la tarifa nueva importa -- la razón que ya daba el código."""
    contrato = ContratoFalso(estado="en_renovacion", fecha_fin=date(2030, 1, 1))
    assert vigencia.de_contrato(contrato, HOY) == "vigente"
    assert vigencia.esta_vivo(contrato, HOY) is True


# ── La fecha decide el resto ──────────────────────────────────────────────

def test_sin_fecha_fin_es_vigente():
    """Un contrato indefinido rige: 138 de 160 en producción están así."""
    contrato = ContratoFalso()
    assert vigencia.de_contrato(contrato, HOY) == "vigente"
    assert vigencia.esta_vivo(contrato, HOY) is True


def test_el_caso_que_los_filtros_viejos_no_veian():
    """`estado='vigente'` con la fecha pasada NO está vivo."""
    contrato = ContratoFalso(estado="vigente", fecha_fin=date(2025, 6, 19))
    assert vigencia.de_contrato(contrato, HOY) == "vencido"
    assert vigencia.esta_vivo(contrato, HOY) is False


def test_por_vencer_sigue_vivo():
    """Vence pronto, pero todavía rige: no se puede excluir."""
    contrato = ContratoFalso(fecha_fin=date(2026, 10, 15))
    assert vigencia.de_contrato(contrato, HOY) == "por_vencer"
    assert vigencia.esta_vivo(contrato, HOY) is True


def test_el_dia_en_que_vence_todavia_cuenta():
    contrato = ContratoFalso(fecha_fin=HOY)
    assert vigencia.esta_vivo(contrato, HOY) is True


def test_el_dia_siguiente_ya_no():
    contrato = ContratoFalso(fecha_fin=date(2026, 9, 16))
    assert vigencia.de_contrato(contrato, HOY) == "vencido"


def test_lejos_de_vencer_es_vigente():
    assert vigencia.de_contrato(ContratoFalso(fecha_fin=date(2030, 1, 1)), HOY) == "vigente"


# ── El filtro de ORM dice lo mismo que la función ─────────────────────────

def test_el_filtro_y_la_funcion_no_pueden_divergir():
    """Si se separan, cada consulta devuelve un conjunto distinto de contratos."""
    from django.db.models import Q

    filtro = vigencia.filtro_vivos(HOY)
    assert isinstance(filtro, Q)
    # Las dos mitades: estado no cerrado, y fecha nula o futura.
    texto = str(filtro)
    assert "estado__in" in texto
    assert "fecha_fin__isnull" in texto
    assert "fecha_fin__gte" in texto


@pytest.mark.parametrize("estado,fecha_fin,vivo", [
    ("vigente", None, True),
    ("vigente", date(2030, 1, 1), True),
    ("vigente", date(2026, 10, 15), True),
    ("vigente", date(2025, 6, 19), False),
    ("en_renovacion", None, True),
    ("terminado", None, False),
    ("terminado", date(2030, 1, 1), False),
])
def test_tabla_de_verdad(estado, fecha_fin, vivo):
    contrato = ContratoFalso(estado=estado, fecha_fin=fecha_fin)
    assert vigencia.esta_vivo(contrato, HOY) is vivo


# ── Las constantes duplicadas ya no existen ───────────────────────────────

def test_las_dos_tuplas_duplicadas_desaparecieron():
    """Eran la misma lista con dos nombres, y ninguna miraba la fecha."""
    from api.v1.monitoreo import queryset as monitoreo_queryset
    from apps.contratos.services import alertas_representacion

    assert not hasattr(monitoreo_queryset, "ESTADOS_CONTRATO_VIVO")
    assert not hasattr(alertas_representacion, "ESTADOS_QUE_AVISAN")
