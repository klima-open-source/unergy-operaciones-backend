"""Tests de AlarmEngine (apps.monitoreo.services.alarmas.alarm_engine), sin BD ni red --
todo el motor opera sobre listas de nodos en memoria + los mapas
node_to_proyecto/proyecto_nombres que en producción arma
scheduler._resolver_mapa_proyectos() vía fronteras.proyecto_id (FK real).

Cubre en particular el cambio de agrupar por proyecto_id (FK resuelto)
en vez de por nombre de nodo normalizado -- auditoría alarmas_monitoreo
2026-08-31."""
from datetime import datetime

import pytz

from apps.comun.config import settings
from apps.monitoreo.services.alarmas.alarm_engine import AlarmEngine, AlarmType, Severity, _group_by_project

TZ = pytz.timezone(settings.TIMEZONE)


def _node(id_, category="ELECTRICAL_GENERATION", status="OK", eae=10.0):
    return {"id": id_, "category": category, "status": status, "eae": eae}


def _mediodia():
    """Hora Colombia dentro de la ventana solar (evaluate() ignora la noche)."""
    return TZ.localize(datetime(2026, 8, 31, 12, 0, 0))


class _EngineEnHorarioSolar(AlarmEngine):
    """Fuerza la evaluación a mediodía sin depender del reloj real -- evita
    tests intermitentes si corren de noche."""
    def evaluate(self, nodes, node_to_proyecto, proyecto_nombres):
        import apps.monitoreo.services.alarmas.alarm_engine as mod
        original = mod.datetime

        class _FixedDatetime(original):
            @classmethod
            def now(cls, tz=None):
                return _mediodia()

        mod.datetime = _FixedDatetime
        try:
            return super().evaluate(nodes, node_to_proyecto, proyecto_nombres)
        finally:
            mod.datetime = original


def test_group_by_project_agrupa_por_proyecto_id_no_por_nombre():
    """Dos nodos (Principal + Respaldo) de un mismo proyecto se agrupan
    porque ambos resuelven al mismo proyecto_id -- no por parsear el nombre."""
    nodes = [_node(101, status="OK"), _node(102, status="ERROR")]
    node_to_proyecto = {101: 7, 102: 7}
    proyecto_nombres = {7: "MGS 0009 El Molino"}

    grupos = _group_by_project(nodes, node_to_proyecto, proyecto_nombres)

    assert len(grupos) == 1
    assert grupos[0]["proyecto_id"] == 7
    assert grupos[0]["name"] == "MGS 0009 El Molino"
    # mejor estado entre los miembros gana (_STATUS_PRIORITY: OK=0 es el más
    # bajo) -- si el respaldo está OK aunque el principal esté en ERROR, el
    # proyecto virtual se reporta OK. Comportamiento preexistente, preservado
    # tal cual por este refactor.
    assert grupos[0]["status"] == "OK"


def test_group_by_project_ignora_nodo_sin_proyecto_resuelto():
    """Un nodo Quoia que no resuelve a ningún proyecto_id (sin frontera
    vinculada) se ignora -- ya no se adivina por nombre."""
    nodes = [_node(999, status="ERROR")]
    grupos = _group_by_project(nodes, node_to_proyecto={}, proyecto_nombres={})
    assert grupos == []


def test_group_by_project_ignora_categoria_no_electrica():
    nodes = [_node(1, category="INVERTER", status="OK")]
    grupos = _group_by_project(nodes, {1: 7}, {7: "X"})
    assert grupos == []


def test_planta_caida_dispara_tras_debounce():
    engine = _EngineEnHorarioSolar()
    node_to_proyecto = {1: 7}
    proyecto_nombres = {7: "Proyecto Siete"}
    nodes_malos = [_node(1, status="NO_DATA")]

    alarmas_por_ciclo = [
        engine.evaluate(nodes_malos, node_to_proyecto, proyecto_nombres)
        for _ in range(4)
    ]
    # Los primeros 3 ciclos no alcanzan DEBOUNCE_POLLS (4) -- sin alarma
    assert all(a.alarm_type != AlarmType.PLANTA_CAIDA for a in alarmas_por_ciclo[0])
    assert all(a.alarm_type != AlarmType.PLANTA_CAIDA for a in alarmas_por_ciclo[1])
    assert all(a.alarm_type != AlarmType.PLANTA_CAIDA for a in alarmas_por_ciclo[2])
    # El 4to ciclo cruza el umbral
    tipos = [a.alarm_type for a in alarmas_por_ciclo[3]]
    assert AlarmType.PLANTA_CAIDA in tipos
    caida = next(a for a in alarmas_por_ciclo[3] if a.alarm_type == AlarmType.PLANTA_CAIDA)
    assert caida.proyecto_id == 7
    assert caida.proyecto_nombre == "Proyecto Siete"
    assert caida.severity == Severity.CRITICAL


def test_planta_caida_no_se_repite_mientras_sigue_activa():
    engine = _EngineEnHorarioSolar()
    node_to_proyecto = {1: 7}
    proyecto_nombres = {7: "Proyecto Siete"}
    nodes_malos = [_node(1, status="ERROR")]

    for _ in range(4):
        engine.evaluate(nodes_malos, node_to_proyecto, proyecto_nombres)
    # Ya disparó en el 4to ciclo -- el 5to, con la misma condición, no repite
    alarmas = engine.evaluate(nodes_malos, node_to_proyecto, proyecto_nombres)
    assert all(a.alarm_type != AlarmType.PLANTA_CAIDA for a in alarmas)


def test_sin_generacion_ok_pero_eae_cero():
    engine = _EngineEnHorarioSolar()
    node_to_proyecto = {1: 7}
    proyecto_nombres = {7: "Proyecto Siete"}
    nodes = [_node(1, status="OK", eae=0)]

    alarmas = engine.evaluate(nodes, node_to_proyecto, proyecto_nombres)
    tipos = [a.alarm_type for a in alarmas]
    assert AlarmType.SIN_GENERACION in tipos
    a = next(a for a in alarmas if a.alarm_type == AlarmType.SIN_GENERACION)
    assert a.severity == Severity.WARNING
    assert a.proyecto_id == 7


def test_recuperacion_tras_caida():
    engine = _EngineEnHorarioSolar()
    node_to_proyecto = {1: 7}
    proyecto_nombres = {7: "Proyecto Siete"}

    for _ in range(4):
        engine.evaluate([_node(1, status="ERROR")], node_to_proyecto, proyecto_nombres)

    alarmas = engine.evaluate([_node(1, status="OK", eae=10)], node_to_proyecto, proyecto_nombres)
    tipos = [a.alarm_type for a in alarmas]
    assert AlarmType.RECUPERACION in tipos
    rec = next(a for a in alarmas if a.alarm_type == AlarmType.RECUPERACION)
    assert rec.proyecto_id == 7
    assert rec.severity == Severity.INFO
    # Tras la recuperación, active_alarms para ese proyecto ya no tiene PLANTA_CAIDA
    assert AlarmType.PLANTA_CAIDA not in engine.active_alarms.get(7, set())


def test_no_recuperacion_sin_planta_caida_confirmada():
    """Regresión: una fluctuación ERROR->OK que nunca llegó a superar el
    debounce (DEBOUNCE_POLLS=4) y por lo tanto nunca disparó PLANTA_CAIDA no
    debe generar una alarma de RECUPERACION -- antes del fix, la condición
    `AlarmType.RECUPERACION not in proj_alarms` era siempre True (RECUPERACION
    nunca se agrega a ese set), así que cualquier fluctuación corta generaba
    ruido de recuperación sin que hubiera una caída real que "recuperar"
    (auditoría 2026-09-01)."""
    engine = _EngineEnHorarioSolar()
    node_to_proyecto = {1: 7}
    proyecto_nombres = {7: "Proyecto Siete"}

    engine.evaluate([_node(1, status="ERROR")], node_to_proyecto, proyecto_nombres)

    alarmas = engine.evaluate([_node(1, status="OK", eae=10)], node_to_proyecto, proyecto_nombres)
    tipos = [a.alarm_type for a in alarmas]
    assert AlarmType.RECUPERACION not in tipos
    assert AlarmType.PLANTA_CAIDA not in tipos


def test_evaluate_de_noche_no_genera_alarmas():
    class _EngineDeNoche(AlarmEngine):
        def evaluate(self, nodes, node_to_proyecto, proyecto_nombres):
            import apps.monitoreo.services.alarmas.alarm_engine as mod
            original = mod.datetime

            class _FixedDatetime(original):
                @classmethod
                def now(cls, tz=None):
                    return TZ.localize(datetime(2026, 8, 31, 22, 0, 0))

            mod.datetime = _FixedDatetime
            try:
                return super().evaluate(nodes, node_to_proyecto, proyecto_nombres)
            finally:
                mod.datetime = original

    engine = _EngineDeNoche()
    alarmas = engine.evaluate([_node(1, status="NO_DATA")], {1: 7}, {7: "Proyecto Siete"})
    assert alarmas == []
