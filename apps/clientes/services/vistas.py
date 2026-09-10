"""Las tres salidas compuestas de Clientes: vista comercial, servicios y panel 360.

Puerto de `vista_comercial`, `list_client_servicios_contratos` y
`get_cliente_panel` de `app/api/v1/clientes.py`. Son agregados, no filas: la vista
solo los devuelve tal cual.

**"Plantas del cliente" es siempre la misma unión** —inversionista, contratante o
prestador de un contrato de servicio, comprador o vendedor de un PPA— y sale de
`panel.proyectos_por_cliente`. Cada sitio que la recalculara a su manera es un
sitio donde el panel 360 y la tabla comercial pueden discrepar.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from django.db.models import Q

from apps.clientes.models import Cliente
from apps.clientes.services.panel import (
    alerta_contratos_por_cliente, contacto_comercial_por_cliente, peor_semaforo,
    proyectos_por_cliente, renovacion_combinada, semaforo_contrato,
    servicios_por_cliente,
)
from apps.contratos.models import ContratoServicio
from apps.plataforma.services.fechas import hoy_col
from apps.ppa.models import PpaContrato, PpaContratoProyecto
from apps.proyectos.models import Proyecto, ProyectoInversionista


def _enlace_documento(documentos) -> str | None:
    """`enlace_drive` de un contrato de servicio y `carpeta_link` de un PPA son la
    misma cosa desde la revisión 122: el documento comercial de `tipo='contrato'`.

    Las dos columnas se eliminaron; el nombre se conserva en la salida para que el
    frontend no cambie. Se lee de la relación YA precargada — buscarlo por fila
    sería un N+1.
    """
    for documento in documentos:
        if documento.tipo == "contrato" and documento.archivo_url:
            return documento.archivo_url
    return None


def _num(v):
    return float(v) if v is not None else None


def _fecha(v):
    return v.isoformat() if v else None


def vista_comercial(hoy: date | None = None) -> list[dict]:
    """Tabla comercial de /clientes: contacto comercial + agregados.

    Devuelve TODOS los clientes sin paginar: el volumen es bajo y el frontend
    filtra y ordena del lado del cliente para respuesta instantánea.
    """
    hoy = hoy or hoy_col()
    clientes = list(
        Cliente.objects.filter(deleted_at__isnull=True).order_by("razon_social_nombre")
    )
    ids = {c.id for c in clientes}
    proys = proyectos_por_cliente(ids)
    servs = servicios_por_cliente(ids, plantas=proys)
    alertas = alerta_contratos_por_cliente(ids, hoy, plantas=proys)
    comerciales = contacto_comercial_por_cliente(ids)

    filas = []
    for c in clientes:
        alerta = alertas.get(c.id, {})
        venc = alerta.get("proximo_vencimiento")
        com = comerciales.get(c.id, {})
        filas.append({
            "id": c.id,
            "razon_social_nombre": c.razon_social_nombre,
            "nit_cedula": c.nit_cedula,
            "tipo_persona": c.tipo_persona,
            "ciudad": c.ciudad,
            "departamento": c.departamento,
            "contacto_comercial_nombre": com.get("nombre"),
            "contacto_comercial_telefono": com.get("telefono"),
            "contacto_comercial_correo": com.get("correo"),
            "contactos_comerciales_extra": com.get("adicionales", 0),
            "num_plantas": len(proys.get(c.id, ())),
            "servicios": sorted(servs.get(c.id, set())),
            "alerta_contrato": alerta.get("alerta"),
            "proximo_vencimiento": _fecha(venc),
        })
    return filas


def _contratos_del_cliente(cliente_id: int, plant_ids: set[int]):
    """Contratos de servicio del cliente: por planta suya O por ID directo.

    `contratante_id`/`prestador_id` casi nunca se pobla —el campo del wizard es
    texto libre—, así que sin el camino por planta el panel 360 y esta lista
    quedaban vacíos aunque el cliente tuviera contratos reales. Caso que lo
    motivó: Quantum es inversionista de GD Sirius y GD Elektra, cuyos contratos
    de representación no lo tienen como contratante.
    """
    criterio = Q(contratante_id=cliente_id) | Q(prestador_id=cliente_id)
    if plant_ids:
        criterio |= Q(proyecto_id__in=plant_ids)
    return list(
        ContratoServicio.objects
        .filter(criterio)
        # `enlace_drive` recorre los documentos de cada contrato: sin precargar
        # son tantas consultas como contratos.
        .prefetch_related("cliente_documentos_comerciales_por_contrato_servicio_id")
    )


def servicios_contratos(cliente_id: int, hoy: date | None = None) -> list[dict]:
    """Servicios que Unergy le presta al cliente, DERIVADOS de los contratos
    reales de sus plantas. Agrupados por tipo de servicio, con semáforo y link.
    Solo lectura."""
    hoy = hoy or hoy_col()
    plant_ids = proyectos_por_cliente({cliente_id}).get(cliente_id, set())
    contratos = _contratos_del_cliente(cliente_id, plant_ids)
    if not contratos:
        return []

    proyectos = _proyectos_por_id({c.proyecto_id for c in contratos if c.proyecto_id})

    def _tarifa(c):
        """La tarifa relevante según el tipo de servicio del contrato."""
        if c.servicio_aplica == "representacion":
            return _num(c.tarifa_representacion)
        if c.servicio_aplica == "cgm":
            return _num(c.tarifa_cgm)
        return _num(c.tarifa_base)

    grupos: dict[str, list] = defaultdict(list)
    for c in contratos:
        grupos[c.servicio_aplica].append({
            "contrato_id": c.id,
            "proyecto_id": c.proyecto_id,
            "proyecto_nombre": proyectos[c.proyecto_id].nombre_comercial
            if c.proyecto_id in proyectos else None,
            "numero_contrato": c.numero_contrato,
            "fecha_inicio": _fecha(c.fecha_inicio),
            "fecha_fin": _fecha(c.fecha_fin),
            "estado": c.estado,
            "semaforo": "vencido" if c.estado == "terminado"
            else semaforo_contrato(c.fecha_fin, hoy),
            "renovacion_automatica": c.renovacion_automatica,
            "tarifa": _tarifa(c),
            "enlace_drive": _enlace_documento(
                c.cliente_documentos_comerciales_por_contrato_servicio_id.all()
            ),
        })

    salida = []
    for serv, filas in grupos.items():
        filas.sort(key=lambda x: (x["fecha_fin"] is None, x["fecha_fin"] or ""))
        salida.append({
            "servicio": serv,
            "num_plantas": len({f["proyecto_id"] for f in filas if f["proyecto_id"]}),
            "num_contratos": len(filas),
            "semaforo": peor_semaforo([f["semaforo"] for f in filas]),
            "contratos": filas,
        })
    salida.sort(key=lambda g: g["servicio"])
    return salida


def _proyectos_por_id(ids: set[int]) -> dict[int, Proyecto]:
    if not ids:
        return {}
    return {
        p.id: p for p in Proyecto.objects.filter(id__in=ids, deleted_at__isnull=True)
    }


def panel_360(cliente: Cliente, hoy: date | None = None) -> dict:
    """Pestaña Resumen del detalle: KPIs, plantas contratadas, condiciones
    económicas, histórico de participación y contratos (servicio + PPA)."""
    hoy = hoy or hoy_col()
    cliente_id = cliente.id

    plant_ids = proyectos_por_cliente({cliente_id}).get(cliente_id, set())
    contratos_serv = _contratos_del_cliente(cliente_id, plant_ids)
    ppas = list(
        PpaContrato.objects
        .filter(deleted_at__isnull=True)
        .filter(Q(comprador_id=cliente_id) | Q(vendedor_id=cliente_id))
        .prefetch_related("documentos_comerciales")
    )
    participaciones = list(
        ProyectoInversionista.objects
        .filter(cliente_id=cliente_id)
        .order_by("proyecto_id", "fecha_inicio")
    )

    # Las plantas de cada PPA, en una consulta para todos.
    plantas_de_ppa: dict[int, set[int]] = defaultdict(set)
    for contrato_id, proyecto_id in PpaContratoProyecto.objects.filter(
        contrato_id__in=[c.id for c in ppas]
    ).values_list("contrato_id", "proyecto_id"):
        plantas_de_ppa[contrato_id].add(proyecto_id)

    proyecto_ids = {r.proyecto_id for r in participaciones}
    proyecto_ids |= {c.proyecto_id for c in contratos_serv if c.proyecto_id}
    for ids in plantas_de_ppa.values():
        proyecto_ids |= ids
    proyectos = _proyectos_por_id(proyecto_ids)

    # ── plantas ──
    plantas = []
    for pid, p in sorted(proyectos.items(), key=lambda kv: kv[1].nombre_comercial or ""):
        serv_planta = [c for c in contratos_serv
                       if c.proyecto_id == pid and c.estado != "terminado"]
        ppa_planta = [c for c in ppas if pid in plantas_de_ppa.get(c.id, ())]
        fechas_fin = [c.fecha_fin for c in serv_planta + ppa_planta if c.fecha_fin]
        part_actual = next(
            (_num(r.porcentaje_participacion) for r in participaciones
             if r.proyecto_id == pid and r.porcentaje_participacion is not None
             and (r.fecha_fin is None or r.fecha_fin >= hoy)),
            None,
        )
        plantas.append({
            "proyecto_id": pid,
            "nombre": p.nombre_comercial,
            "estado": p.estado,
            "potencia_kwp": _num(p.potencia_ac_kw),
            "fecha_fin_contrato": _fecha(max(fechas_fin) if fechas_fin else None),
            "renovacion_automatica": renovacion_combinada(
                [c.renovacion_automatica for c in serv_planta + ppa_planta]
            ),
            "servicios": sorted(
                {c.servicio_aplica for c in serv_planta}
                | ({"ppa"} if ppa_planta else set())
            ),
            "participacion_actual": part_actual,
            "semaforo": peor_semaforo(
                [semaforo_contrato(c.fecha_fin, hoy) for c in serv_planta + ppa_planta]
            ),
        })

    # ── histórico de participación (todas las filas, vigentes y cerradas) ──
    historico = [{
        "proyecto_id": r.proyecto_id,
        "proyecto_nombre": proyectos[r.proyecto_id].nombre_comercial
        if r.proyecto_id in proyectos else None,
        "fecha_inicio": _fecha(r.fecha_inicio),
        "fecha_fin": _fecha(r.fecha_fin),
        "porcentaje": _num(r.porcentaje_participacion),
    } for r in participaciones]

    # ── condiciones económicas (un renglón por contrato de servicio) ──
    condiciones = [{
        "contrato_id": c.id,
        "proyecto_id": c.proyecto_id,
        "proyecto_nombre": proyectos[c.proyecto_id].nombre_comercial
        if c.proyecto_id in proyectos else None,
        "servicio": c.servicio_aplica,
        "tarifa_representacion": _num(c.tarifa_representacion),
        "tarifa_cgm": _num(c.tarifa_cgm),
        "tarifa_base": _num(c.tarifa_base),
        "indice_indexacion": c.indice_indexacion,
        "fecha_indexacion": _fecha(c.fecha_indexacion),
    } for c in contratos_serv]

    # ── contratos unificados (servicio + PPA) ──
    contratos = [{
        "id": c.id,
        "fuente": "servicio",
        "tipo": c.servicio_aplica,
        "numero": c.numero_contrato,
        "proyectos": [proyectos[c.proyecto_id].nombre_comercial]
        if c.proyecto_id in proyectos else [],
        "fecha_inicio": _fecha(c.fecha_inicio),
        "fecha_fin": _fecha(c.fecha_fin),
        "estado": c.estado,
        "semaforo": "vencido" if c.estado == "terminado"
        else semaforo_contrato(c.fecha_fin, hoy),
        "renovacion_automatica": c.renovacion_automatica,
        "link": _enlace_documento(
            c.cliente_documentos_comerciales_por_contrato_servicio_id.all()
        ),
    } for c in contratos_serv]
    contratos += [{
        "id": c.id,
        "fuente": "ppa",
        "tipo": "ppa",
        "numero": c.numero_codigo_contrato or c.nombre_interno,
        "proyectos": [
            proyectos[pid].nombre_comercial
            for pid in plantas_de_ppa.get(c.id, ()) if pid in proyectos
        ],
        "fecha_inicio": _fecha(c.fecha_inicio),
        "fecha_fin": _fecha(c.fecha_fin),
        "estado": None,
        "semaforo": semaforo_contrato(c.fecha_fin, hoy),
        "renovacion_automatica": c.renovacion_automatica,
        "link": _enlace_documento(c.documentos_comerciales.all()),
    } for c in ppas]
    contratos.sort(key=lambda x: (x["fecha_fin"] is None, x["fecha_fin"] or ""))

    # ── KPIs ──
    activos = [x for x in contratos if x["semaforo"] != "vencido"]
    vencimientos = [x["fecha_fin"] for x in activos if x["fecha_fin"]]
    return {
        "kpis": {
            "num_plantas": len(plantas),
            "contratos_activos": len(activos),
            "servicios": sorted({x["tipo"] for x in contratos}),
            "proximo_vencimiento": min(vencimientos) if vencimientos else None,
        },
        "plantas": plantas,
        "participaciones_historico": historico,
        "condiciones": condiciones,
        "contratos": contratos,
    }
