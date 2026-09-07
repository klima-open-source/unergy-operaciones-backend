"""Las respuestas del puente `_legacy` de Monitoreo.

`_legacy` reemplazó un Google Apps Script y por eso no es REST: un solo endpoint
con `?action=`. Se conserva la forma tal cual — el frontend de Fallas la llama
así y cambiarla es un trabajo aparte, no un efecto colateral de migrar de
framework.
"""

import calendar
import json
import logging
from datetime import date

from apps.contratos import models as ct_models
from apps.energia.services import unergy_api
from apps.monitoreo.services import solenium_inversores
from apps.proyectos import models as py_models
from apps.proyectos.services import portafolios as portafolios_service

logger = logging.getLogger("operaciones.monitoreo")

# La disponibilidad garantizada es la misma en todos los contratos de O&M.
DISPONIBILIDAD_GARANTIZADA_PCT = "97"
PRESTADOR_POR_DEFECTO = "Unergy S.A.S."
ESTADOS_CONTRATO_VIVO = ("vigente", "en_renovacion")
# "operacion" NUNCA se usa como `servicio_aplica`: el contrato de O&M real es
# "mantenimiento". Filtrar por "operacion" devolvía 0 filas siempre.
SERVICIO_OM = "mantenimiento"


def proyectos_en_operacion():
    return py_models.Proyecto.objects.filter(
        sub_project__isnull=False, estado="en_operacion"
    ).order_by("nombre_comercial")


def build_projects() -> dict:
    return {
        "ok": True,
        "projects": [
            {
                "sub_project": p.sub_project,
                "nombre_comercial": p.nombre_comercial,
                "municipio": p.municipio or "—",
                "departamento": p.departamento or "—",
                "potencia_instalada_kwp": (
                    float(p.potencia_instalada_kwp)
                    if p.potencia_instalada_kwp else None
                ),
                "estado": p.estado,
                "project_id_solenium": p.project_id_solenium or "",
            }
            for p in proyectos_en_operacion()
        ],
    }


def build_portfolios() -> dict:
    """Agrupamiento por portafolio. El error se devuelve, no se levanta.

    El puente responde `{ok: false}` en vez de un 500 porque el frontend lo
    trata como un panel más de la pantalla: un fallo acá no debe tumbar Fallas.
    """
    try:
        return {"ok": True, "portfolios": portafolios_service.agrupamiento()}
    except Exception as exc:
        logger.exception("fallo al agrupar portafolios")
        return {"ok": False, "error": str(exc), "portfolios": {}}


def _contrato_om(proyecto):
    return (
        ct_models.ContratoServicio.objects
        .filter(
            proyecto=proyecto, servicio_aplica=SERVICIO_OM,
            estado__in=ESTADOS_CONTRATO_VIVO,
        )
        .first()
    )


def _contrato_a_dict(contrato, proyecto) -> dict:
    return {
        "sub_project": proyecto.sub_project,
        "nombre_comercial": proyecto.nombre_comercial,
        "disponibilidad_garantizada_pct": DISPONIBILIDAD_GARANTIZADA_PCT,
        "contratista": contrato.prestador_nombre or PRESTADOR_POR_DEFECTO,
        # La tarifa está mensual; el valor del año 1 son doce meses.
        "valor_estimado_ano1_cop": (
            str(round(float(contrato.tarifa_base) * 12))
            if contrato.tarifa_base else "0"
        ),
        "garantias_equipos": "",
        "numero_contrato": contrato.numero_contrato or "",
    }


def build_all_contratos() -> dict:
    contratos = []
    consulta = (
        ct_models.ContratoServicio.objects
        .filter(servicio_aplica=SERVICIO_OM, estado__in=ESTADOS_CONTRATO_VIVO)
        .select_related("proyecto")
    )
    for contrato in consulta:
        proyecto = contrato.proyecto
        # Sin `sub_project` el frontend no puede indexar la fila.
        if not proyecto or not proyecto.sub_project:
            continue
        fila = _contrato_a_dict(contrato, proyecto)
        fila.update({
            "fecha_inicio": (
                contrato.fecha_inicio.isoformat() if contrato.fecha_inicio else ""
            ),
            "fecha_fin": (
                contrato.fecha_fin.isoformat() if contrato.fecha_fin else ""
            ),
            "project_id_solenium": proyecto.project_id_solenium or "",
        })
        contratos.append(fila)
    return {"ok": True, "contratos": contratos}


def build_generation(sub_project: str, desde: date, hasta: date) -> dict:
    """Generación del período más la línea base P50/P90/P99 del proyecto."""
    (pedir_desde, pedir_hasta), (desde_dt, hasta_dt) = unergy_api.ventana_utc(
        desde, hasta
    )
    try:
        lecturas = unergy_api.lecturas_con_respaldo(
            unergy_api.token(), sub_project, pedir_desde, pedir_hasta
        )
    except Exception as exc:
        return {"ok": False, "error": f"Error API Unergy: {exc}"}

    proyecto = py_models.Proyecto.objects.filter(sub_project=sub_project).first()
    return {
        "ok": True,
        "data": unergy_api.deltas(lecturas, desde_dt, hasta_dt),
        "simulation": _simulacion(proyecto, desde),
    }


def _lista_de_kwh(valor):
    """Normaliza JSONB o cadena JSON a lista. Cubre datos históricos."""
    if valor is None or isinstance(valor, list):
        return valor
    if isinstance(valor, str):
        try:
            decodificado = json.loads(valor)
        except Exception:
            return None
        return decodificado if isinstance(decodificado, list) else None
    return None


def _simulacion(proyecto, desde: date) -> dict | None:
    """Línea base del mes: los doce valores mensuales, indexados por mes."""
    if proyecto is None or not (
        proyecto.p90_mensual_kwh or proyecto.p50_mensual_kwh
    ):
        return None
    try:
        mes = desde.month
        def del_mes(campo):
            lista = _lista_de_kwh(getattr(proyecto, campo, None)) or [None] * 12
            return lista[mes - 1] if len(lista) >= mes else None

        p90, p50, p99 = del_mes("p90_mensual_kwh"), del_mes("p50_mensual_kwh"), del_mes("p99_mensual_kwh")
        dias = calendar.monthrange(desde.year, mes)[1]
        return {
            "p90_monthly": p90,
            "p50_monthly": p50,
            "p99_monthly": p99,
            "p90_daily": round(p90 / dias, 1) if p90 else None,
        }
    except Exception:
        # No se silencia: con un P50/P90 corrupto el tablero mostraría la
        # generación real sin línea base y sin ninguna señal del fallo.
        logger.exception(
            "fallo al parsear la simulación P90/P50 proyecto_id=%s",
            getattr(proyecto, "id", "?"),
        )
        return None


def build_fmo(sub_project: str, desde: date | None, hasta: date | None) -> dict:
    """Contrato de O&M e inversores de una planta.

    Ya no devuelve `mantenimientos`: la tabla se elimino el 2026-09-07 (0 filas
    y sin forma de crear un registro). La seccion 5 del informe FMO ya trataba
    la clave ausente como lista vacia, asi que imprime el mismo aviso de
    "Sin registros" que imprimia con la tabla vacia.
    """
    proyecto = py_models.Proyecto.objects.filter(sub_project=sub_project).first()
    if proyecto is None:
        return {
            "ok": True, "contrato": None, "inverters": [],
            "inverters_error": None,
        }

    contrato = _contrato_om(proyecto)
    inversores, error = solenium_inversores.inversores(proyecto)

    return {
        "ok": True,
        "contrato": _contrato_a_dict(contrato, proyecto) if contrato else None,
        "inverters": inversores,
        "inverters_error": error,
    }
