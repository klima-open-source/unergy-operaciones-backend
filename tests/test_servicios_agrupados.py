"""Agrupación de servicios y el endpoint `GET /api/v1/servicios`.

Cubre lo que la vista Servicios necesita y que hoy el backend no daba: los tres
grupos siempre presentes, los conteos que distinguen contratos de plantas, y el
catálogo que el front deja de mantener por su cuenta.
"""

import os
from datetime import date

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

# Ver `tests/test_contratos_unificado.py`: el arranque va a nivel de módulo
# porque los imports de abajo cargan modelos durante la colección.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("SECRET_KEY", "x" * 40)
django.setup()

from apps.contratos.services import grupos, unificado  # noqa: E402

HOY = date(2026, 9, 16)


class ProyectoFalso:
    def __init__(self, id, nombre_comercial="Planta", tipo_proyecto="gd"):
        self.id = id
        self.nombre_comercial = nombre_comercial
        self.tipo_proyecto = tipo_proyecto


class ContratoFalso:
    def __init__(self, servicio_aplica, id=1, fecha_fin=None, estado="vigente",
                 tarifa_base=None, tarifa_representacion=None, tarifa_cgm=None):
        self.id = id
        self.servicio_aplica = servicio_aplica
        self.numero_contrato = None
        self.fecha_inicio = None
        self.fecha_fin = fecha_fin
        self.estado = estado
        self.renovacion_automatica = None
        self.contratante_nombre = None
        self.prestador_nombre = None
        self.tarifa_base = tarifa_base
        self.tarifa_representacion = tarifa_representacion
        self.tarifa_cgm = tarifa_cgm


class PpaFalso:
    def __init__(self, id=100, tipo_contrato="venta", fecha_fin=None):
        self.id = id
        self.tipo_contrato = tipo_contrato
        self.numero_codigo_contrato = "PPA-1"
        self.nombre_interno = None
        self.fecha_inicio = None
        self.fecha_fin = fecha_fin
        self.renovacion_automatica = None
        self.comprador_nombre = None
        self.vendedor_nombre = None


def _servicio(contrato, proyecto=None):
    return unificado.desde_contrato_servicio(contrato, HOY, proyecto=proyecto)


def _ppa(contrato, proyectos=()):
    return unificado.desde_ppa(contrato, HOY, proyectos=proyectos)


# ── Los tres grupos ───────────────────────────────────────────────────────

def test_los_tres_grupos_salen_aunque_no_haya_nada():
    """Una pestaña que desaparece se lee como un error de carga."""
    salida = unificado.agrupar([])
    assert [g["grupo"] for g in salida] == list(grupos.ORDEN_GRUPOS)
    for grupo in salida:
        assert grupo["servicios"] == []
        assert grupo["conteos"] == {"contratos": 0, "plantas": 0}
        assert grupo["semaforo"] is None


def test_cada_servicio_cae_en_su_grupo():
    planta = ProyectoFalso(7)
    salida = unificado.agrupar([
        _ppa(PpaFalso(), proyectos=[planta]),
        _servicio(ContratoFalso("representacion", id=2,
                                tarifa_representacion=1), proyecto=planta),
        _servicio(ContratoFalso("mantenimiento", id=3), proyecto=planta),
    ])
    por_grupo = {g["grupo"]: g for g in salida}
    assert por_grupo["ppa"]["conteos"]["contratos"] == 1
    assert por_grupo["representacion_cgm"]["conteos"]["contratos"] == 1
    assert por_grupo["operacion"]["conteos"]["contratos"] == 1


def test_cada_grupo_expone_sus_subservicios():
    por_grupo = {g["grupo"]: g["subservicios"] for g in unificado.agrupar([])}
    assert por_grupo["operacion"] == ["mantenimiento", "arriendo", "internet"]


def test_un_contrato_sin_grupo_reconocible_no_rompe_la_agrupacion():
    salida = unificado.agrupar([_servicio(ContratoFalso("promotor"))])
    assert sum(g["conteos"]["contratos"] for g in salida) == 0


# ── Conteos: contratos y plantas ──────────────────────────────────────────

def test_operacion_tres_contratos_una_planta():
    planta = ProyectoFalso(7)
    servicios = [
        _servicio(ContratoFalso(tipo, id=i), proyecto=planta)
        for i, tipo in enumerate(["mantenimiento", "arriendo", "internet"], 1)
    ]
    grupo = {g["grupo"]: g for g in unificado.agrupar(servicios)}["operacion"]
    assert grupo["conteos"] == {"contratos": 3, "plantas": 1}


def test_ppa_un_contrato_cinco_plantas():
    plantas = [ProyectoFalso(i) for i in range(1, 6)]
    grupo = {g["grupo"]: g for g in unificado.agrupar([
        _ppa(PpaFalso(), proyectos=plantas)
    ])}["ppa"]
    assert grupo["conteos"] == {"contratos": 1, "plantas": 5}


# ── Semáforo y orden ──────────────────────────────────────────────────────

def test_el_semaforo_del_grupo_es_el_peor_de_sus_contratos():
    servicios = [
        _servicio(ContratoFalso("mantenimiento", id=1, fecha_fin=date(2030, 1, 1))),
        _servicio(ContratoFalso("arriendo", id=2, fecha_fin=date(2020, 1, 1))),
    ]
    grupo = {g["grupo"]: g for g in unificado.agrupar(servicios)}["operacion"]
    assert grupo["semaforo"] == "vencido"


def test_los_que_vencen_antes_salen_primero_y_los_indefinidos_al_final():
    servicios = [
        _servicio(ContratoFalso("mantenimiento", id=1, fecha_fin=None)),
        _servicio(ContratoFalso("arriendo", id=2, fecha_fin=date(2030, 1, 1))),
        _servicio(ContratoFalso("internet", id=3, fecha_fin=date(2027, 1, 1))),
    ]
    grupo = {g["grupo"]: g for g in unificado.agrupar(servicios)}["operacion"]
    assert [s["contrato_id"] for s in grupo["servicios"]] == [3, 2, 1]


# ── El caso que hoy es invisible ──────────────────────────────────────────

def test_un_contrato_de_representacion_y_cgm_aparece_con_los_dos():
    contrato = ContratoFalso(
        "representacion", id=47, tarifa_representacion=1, tarifa_cgm=2
    )
    grupo = {g["grupo"]: g for g in unificado.agrupar([_servicio(contrato)])}
    fila = grupo["representacion_cgm"]["servicios"][0]
    assert fila["subservicios"] == ["representacion", "cgm"]


# ── El catálogo que consume el front ──────────────────────────────────────

def test_el_catalogo_trae_los_tres_grupos_con_sus_subservicios():
    catalogo = grupos.catalogo()
    assert [g["grupo"] for g in catalogo] == ["ppa", "representacion_cgm", "operacion"]
    assert catalogo[0]["subservicios"] == ["compra", "venta"]


def test_el_catalogo_y_la_agrupacion_dicen_lo_mismo():
    """Si se desincronizan, el front filtra por subservicios que no existen."""
    del_catalogo = {g["grupo"]: g["subservicios"] for g in grupos.catalogo()}
    de_agrupar = {g["grupo"]: g["subservicios"] for g in unificado.agrupar([])}
    assert del_catalogo == de_agrupar


# ── Rutas registradas ─────────────────────────────────────────────────────

def test_las_dos_rutas_existen():
    from django.urls import resolve

    assert resolve("/api/v1/servicios").func is not None
    assert resolve("/api/v1/servicios/catalogo").func is not None


# ── Las relaciones que la consulta precarga ───────────────────────────────

def test_las_relaciones_que_precarga_la_consulta_existen():
    """`prefetch_related` con un nombre equivocado falla en runtime, no al importar.

    Sin esto, un `related_name` renombrado rompe el endpoint en producción y
    ninguna prueba se entera: las de arriba no tocan el ORM.
    """
    from apps.contratos.models import ContratoServicio
    from apps.contratos.services import consulta
    from apps.ppa.models import PpaContrato

    del_ppa = {f.name for f in PpaContrato._meta.get_fields()}
    assert "documentos_comerciales" in del_ppa
    assert "proyectos_vinculados" in del_ppa

    del_contrato = {f.name for f in ContratoServicio._meta.get_fields()}
    assert consulta._DOCS_SERVICIO in del_contrato


def test_la_consulta_arma_los_tres_grupos_sin_tocar_la_base(monkeypatch):
    """`agrupados()` delega en `listar()`; acá se verifica el ensamble."""
    from apps.contratos.services import consulta

    monkeypatch.setattr(consulta, "listar", lambda **kwargs: [])
    assert [g["grupo"] for g in consulta.agrupados()] == list(grupos.ORDEN_GRUPOS)


# ── Plantas borradas lógicamente ──────────────────────────────────────────

def test_una_planta_borrada_no_se_muestra():
    """`proyectos.deleted_at` es borrado lógico: la fila sigue en la base.

    El resto del sistema las omite y deja el contrato sin planta; si no se
    filtraran, la vista mostraría plantas que ya no existen.
    """
    from apps.contratos.services import consulta

    class ProyectoBorrado:
        id, nombre_comercial, tipo_proyecto = 9, "Borrada", "gd"
        deleted_at = date(2026, 1, 1)

    class ProyectoVivo:
        id, nombre_comercial, tipo_proyecto = 7, "Villanueva", "gd"
        deleted_at = None

    assert consulta._vigente(ProyectoBorrado()) is None
    assert consulta._vigente(ProyectoVivo()) is not None
    assert consulta._vigente(None) is None
