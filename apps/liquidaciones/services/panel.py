"""Los datos que el resumen espejo necesita de la base.

El armado en sí es puro y vive en `resumen_panel.py`; acá van las consultas.
Todas van EN LOTE: la versión anterior resolvía el cliente de cada
`proyecto_inversionista` fila por fila.
"""

from datetime import date

from django.db.models import Exists, OuterRef, Q

from apps.clientes import models as cl_models
from apps.contabilidad import models as cb_models
from apps.contratos import models as ct_models
from apps.contratos.services import vigencia as vigencia_service
from apps.liquidaciones import models as lq_models
from apps.plataforma.services.fechas import hoy_col
from apps.proyectos import models as py_models


def normalizar_periodo(periodo: str) -> tuple[str, date]:
    """`"2026-5"` → `("2026-05", date(2026, 5, 1))`."""
    anio, mes = periodo.strip().split("-")
    return f"{int(anio):04d}-{int(mes):02d}", date(int(anio), int(mes), 1)


def paneles_de(periodo_norm: str, tipo: str):
    return (
        cb_models.PanelContable.objects
        .filter(periodo=periodo_norm, tipo=tipo)
        .prefetch_related("lineas").order_by("id")
    )


def paneles_en_rango(desde: str, hasta: str, tipo: str):
    return (
        cb_models.PanelContable.objects
        .filter(periodo__gte=desde, periodo__lte=hasta, tipo=tipo)
        .prefetch_related("lineas").order_by("periodo", "id")
    )


def nombres_y_tipos(proyecto_ids) -> tuple[dict, dict]:
    """Nombre y tipo SOLO de los proyectos del período, no de toda la tabla."""
    nombres, tipos = {}, {}
    if not proyecto_ids:
        return nombres, tipos
    for pid, nombre, tipo in py_models.Proyecto.objects.filter(
        id__in=proyecto_ids
    ).values_list("id", "nombre_comercial", "tipo_proyecto"):
        nombres[pid] = nombre
        tipos[pid] = tipo
    return nombres, tipos


def liquidacion_por_proyecto(proyecto_ids, periodo: date) -> dict:
    """`{proyecto_id: liquidacion_id}` — el PRIMERO por proyecto."""
    salida = {}
    if not proyecto_ids:
        return salida
    filas = lq_models.Liquidacion.objects.filter(
        proyecto_id__in=proyecto_ids, periodo=periodo, deleted_at__isnull=True
    ).order_by("id").values_list("id", "proyecto_id")
    for liquidacion_id, proyecto_id in filas:
        salida.setdefault(proyecto_id, liquidacion_id)
    return salida


def liquidaciones_en_rango(proyecto_ids, desde: date, hasta: date) -> dict:
    """`{(proyecto_id, "YYYY-MM"): liquidacion_id}`."""
    salida = {}
    if not proyecto_ids:
        return salida
    filas = lq_models.Liquidacion.objects.filter(
        proyecto_id__in=proyecto_ids, periodo__gte=desde, periodo__lte=hasta,
        deleted_at__isnull=True,
    ).order_by("id").values_list("id", "proyecto_id", "periodo")
    for liquidacion_id, proyecto_id, periodo in filas:
        if periodo:
            salida.setdefault(
                (proyecto_id, periodo.strftime("%Y-%m")), liquidacion_id
            )
    return salida


def clientes_por_inversionista(pi_ids) -> dict:
    """`{proyecto_inversionista_id: {cliente_id, nombre, tasas…}}`.

    Las cuatro tasas alimentan el desglose de impuestos de las facturas de
    servicio, que se calcula en tiempo de LECTURA.
    """
    salida = {}
    if not pi_ids:
        return salida
    filas = (
        py_models.ProyectoInversionista.objects
        .filter(id__in=pi_ids)
        .select_related("cliente")
        .values_list(
            "id", "cliente_id", "cliente__razon_social_nombre",
            "cliente__iva_pct", "cliente__retencion_pct",
            "cliente__reteiva_pct", "cliente__reteica_pct",
        )
    )
    for pi_id, cliente_id, razon, iva, retencion, reteiva, reteica in filas:
        salida[pi_id] = {
            "cliente_id": cliente_id,
            "cliente_nombre": razon,
            "iva_pct": float(iva) if iva is not None else None,
            "retencion_pct": float(retencion) if retencion is not None else None,
            "reteiva_pct": float(reteiva) if reteiva is not None else None,
            "reteica_pct": float(reteica) if reteica is not None else None,
        }
    return salida


def proyectos_sin_panel(proyecto_ids_con_panel) -> list[dict]:
    """Minigranjas en operación que REPRESENTAMOS y no tienen panel del mes.

    Es una alerta de «puede faltar cargar el ER». Criterio deliberadamente
    SEGURO —mejor alertar de más que dejar de liquidar algo representado—: se
    considera representado si el flag `srv_representacion` está activo O existe
    un contrato de representación vigente.

    Los dos se contradicen en la práctica (El Roble y Chima Oriente tienen el
    flag apagado pero contrato vigente), así que se conservan por seguridad.
    """
    # "Contrato vivo" es `vigencia.filtro_vivos`, no `estado="firmado"`: el estado
    # dice lo que alguien decidio, y la fecha dice si todavia rige. Un contrato
    # firmado que ya vencio no representa a nadie.
    tiene_contrato = Exists(
        ct_models.ContratoServicio.objects.filter(
            vigencia_service.filtro_vivos(hoy_col()),
            proyecto_id=OuterRef("id"),
            servicio_aplica="representacion",
        )
    )
    consulta = py_models.Proyecto.objects.filter(
        estado="en_operacion", tipo_proyecto="minigranja"
    ).annotate(con_contrato=tiene_contrato).filter(
        Q(srv_representacion=True) | Q(con_contrato=True)
    )
    if proyecto_ids_con_panel:
        consulta = consulta.exclude(id__in=proyecto_ids_con_panel)
    return [
        {"proyecto_id": pid, "proyecto": nombre}
        for pid, nombre in consulta.values_list("id", "nombre_comercial")
    ]


def ids_de_inversionista(paneles) -> set:
    return {
        linea.proyecto_inversionista_id
        for panel in paneles for linea in panel.lineas.all()
        if linea.proyecto_inversionista_id is not None
    }
