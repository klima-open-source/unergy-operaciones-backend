"""Consultas del Informe de Puesta en Marcha."""

from apps.om import models as om_models
from apps.om.services import checklist, evidencia, forma_ficha, vivo
from apps.proyectos import models as py_models


def proyectos_con_informe():
    """Minigranjas en operación con servicio de operación.

    Mismo filtro que la pantalla de Inicio de Operación: el informe de puesta en
    marcha solo aplica a las plantas que operamos nosotros.
    """
    return py_models.Proyecto.objects.filter(
        srv_operacion=True,
        tipo_proyecto="minigranja",
        estado="en_operacion",
        deleted_at__isnull=True,
    ).order_by("nombre_comercial")


def build_listado() -> list[dict]:
    """El listado con el estado global de cada proyecto.

    Las fichas se traen en UNA consulta y se indexan por proyecto: pedir la de
    cada fila sería un N+1 sobre una pantalla que lista toda la flota.
    """
    fichas = {
        f.proyecto_id: f for f in om_models.ProyectoInformeOm.objects.all()
    }
    return [
        {
            "id": proyecto.id,
            "nombre_comercial": proyecto.nombre_comercial,
            "municipio": proyecto.municipio,
            "departamento": proyecto.departamento,
            "potencia_ac_kw": (
                float(proyecto.potencia_ac_kw)
                if proyecto.potencia_ac_kw is not None else None
            ),
            "tiene_ficha": proyecto.id in fichas,
            "estado_global": checklist.kpis(fichas.get(proyecto.id))["estado_global"],
        }
        for proyecto in proyectos_con_informe()
    ]


def build_detalle(proyecto, ficha) -> dict:
    """Proyecto + ficha + semáforos + KPIs + datos en vivo + evidencia."""
    return {
        "proyecto": proyecto,
        "ficha": forma_ficha.leer(ficha),
        "kpis": checklist.kpis(ficha),
        "inversores": vivo.inversores(proyecto),
        **checklist.semaforos(ficha),
        "frontera_live": vivo.frontera(proyecto),
        "evidencia_relacionada": evidencia.relacionada(ficha),
    }
