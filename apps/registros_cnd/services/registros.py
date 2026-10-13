"""Orquestacion con la base de datos para "Registros CND/ASIC".

Puerto de `app/services/registros_cnd/service.py` al ORM de Django. Aqui vive lo
que toca la base: crear un registro con su estructura (etapas + hitos), construir
el resumen "en que va el proyecto X", registrar una transicion validada por la
maquina de estados y recomputar alertas (upsert por `dedupe_key`).

La logica pura (`dominio`, `avance`, `state_machine`, `validaciones_93`,
`alertas`, `correos`) se movio sin tocarla: no sabia de la sesion de SQLAlchemy
y tampoco sabe del ORM de Django.
"""

from __future__ import annotations

from datetime import date, datetime

from django.db import transaction
from django.utils import timezone

from apps.contratos.services import plantas
from apps.plataforma.services.fechas import hoy_col
from apps.proyectos.models import Proyecto
from apps.registros_cnd.models import (
    RegistroAlerta, RegistroConexion, RegistroEtapa, RegistroHito, RegistroTransicion,
)
from apps.registros_cnd.services import alertas as alertas_mod
from apps.registros_cnd.services import state_machine as sm
from apps.registros_cnd.services.avance import (
    HitoAvance, avance_por_etapa, calcular_avance_pct, siguiente_hito_pendiente, suma_pesos,
)
from apps.registros_cnd.services.dominio import (
    ETAPAS_ACTUALES, ETAPAS_FUTURAS, ETIQUETAS_ETAPA, HITOS, HITOS_POR_KEY,
    Estado, Etapa,
)

# Todo lo que `construir_resumen` recorre. Sin esto son 5 consultas por fila y
# el listado hace una fila por proyecto de la plataforma.
RELACIONES_DEL_RESUMEN = ("etapas", "hitos", "alertas", "equipos")


def con_relaciones(qs=None):
    """El queryset de registros con todo lo que el resumen necesita precargado."""
    qs = RegistroConexion.objects.all() if qs is None else qs
    return qs.select_related("proyecto__operador_red").prefetch_related(*RELACIONES_DEL_RESUMEN)


# ---------------------------------------------------------------------------
# Creacion
# ---------------------------------------------------------------------------
def crear_registro(proyecto_id: int, **campos) -> RegistroConexion:
    """Crea un registro sobre un Proyecto e inicializa TODAS sus etapas (actuales +
    futuras) en su estado inicial, y todos los hitos 1a-8c con su peso por defecto.

    Lanza ValueError si el proyecto no existe o ya tiene registro.
    """
    if not Proyecto.objects.filter(pk=proyecto_id).exists():
        raise ValueError(f"El proyecto {proyecto_id} no existe")
    existente = RegistroConexion.objects.filter(proyecto_id=proyecto_id).first()
    if existente is not None:
        raise ValueError(f"El proyecto {proyecto_id} ya tiene un registro de conexion (id={existente.id})")

    with transaction.atomic():
        registro = RegistroConexion.objects.create(proyecto_id=proyecto_id, **campos)
        RegistroEtapa.objects.bulk_create([
            RegistroEtapa(
                registro_id=registro.id,
                etapa=etapa,
                estado_actual=sm.get_etapa_def(etapa)["inicial"],
            )
            for etapa in [*ETAPAS_ACTUALES, *ETAPAS_FUTURAS]
        ])
        RegistroHito.objects.bulk_create([
            RegistroHito(
                registro_id=registro.id,
                hito=h["key"],
                peso_pct=float(h["peso_default"]),
                completado=False,
            )
            for h in HITOS
        ])
    return registro


def get_or_create_registro(proyecto_id: int) -> RegistroConexion:
    """Devuelve el registro del proyecto, creandolo (con etapas + hitos) si no existe.

    Materializa el seguimiento la primera vez que se abre un proyecto. Lanza ValueError
    si el proyecto no existe.
    """
    reg = RegistroConexion.objects.filter(proyecto_id=proyecto_id).first()
    if reg is not None:
        return reg
    return crear_registro(proyecto_id)


# ---------------------------------------------------------------------------
# Resumen "en que va el proyecto X"
# ---------------------------------------------------------------------------
def _to_hito_avance(hitos_orm) -> list[HitoAvance]:
    return [HitoAvance(hito=h.hito, peso_pct=h.peso_pct, completado=h.completado) for h in hitos_orm]


def _enum_val(v) -> str | None:
    if v is None:
        return None
    return getattr(v, "value", v)


def construir_resumen(registro: RegistroConexion) -> dict:
    """Construye el resumen completo del registro (avance, estado por etapa,
    siguiente paso con responsable, bloqueos, alertas pendientes)."""
    proyecto = registro.proyecto
    hitos = list(registro.hitos.all())
    hitos_av = _to_hito_avance(hitos)
    por_etapa_av = {a.etapa: a for a in avance_por_etapa(hitos_av)}
    etapa_rows = {e.etapa: e for e in registro.etapas.all()}

    por_etapa = []
    for etapa in ETAPAS_ACTUALES:
        row = etapa_rows.get(etapa)
        av = por_etapa_av[etapa]
        por_etapa.append({
            "etapa": etapa,
            "etiqueta": ETIQUETAS_ETAPA[etapa],
            "estado_actual": row.estado_actual if row else sm.get_etapa_def(etapa)["inicial"],
            "bloqueada": row.bloqueada if row else False,
            "causa_bloqueo": row.causa_bloqueo if row else None,
            "responsable_actual": row.responsable_actual if row else None,
            "ganado_pct": av.ganado_pct,
            "total_pct": av.total_pct,
            "completos": av.completos,
            "total_hitos": av.total_hitos,
        })

    # Siguiente paso: primer hito pendiente en el orden canonico.
    sig_hito = siguiente_hito_pendiente(hitos_av)
    siguiente_paso = None
    if sig_hito:
        meta = HITOS_POR_KEY[sig_hito]
        row = etapa_rows.get(meta["etapa"])
        estado_actual = row.estado_actual if row else sm.get_etapa_def(meta["etapa"])["inicial"]
        proximos = sm.transiciones_permitidas(meta["etapa"], estado_actual)
        responsable = (row.responsable_actual if row and row.responsable_actual else None) or (
            sm.responsable_de_estado(meta["etapa"], proximos[0]) if proximos else None
        )
        siguiente_paso = {
            "hito": sig_hito,
            "codigo": sig_hito,
            "descripcion": meta["descripcion"],
            "etapa": meta["etapa"],
            "etiqueta_etapa": ETIQUETAS_ETAPA[meta["etapa"]],
            "responsable": responsable,
        }

    bloqueos = [
        {
            "etapa": e["etapa"],
            "etiqueta": e["etiqueta"],
            "motivo": "Vigencia vencida" if e["estado_actual"] == Estado.VENCIDO
            else (e["causa_bloqueo"] or "Bloqueada (sin causa registrada)"),
        }
        for e in por_etapa
        if e["bloqueada"] or e["estado_actual"] == Estado.VENCIDO
    ]

    hitos_out = sorted(
        (
            {
                "hito": h.hito,
                "codigo": HITOS_POR_KEY.get(h.hito, {}).get("key", h.hito),
                "descripcion": HITOS_POR_KEY.get(h.hito, {}).get("descripcion", ""),
                "etapa": HITOS_POR_KEY.get(h.hito, {}).get("etapa", Etapa.ETAPA_1_CREG174_AMBITO),
                "peso_pct": h.peso_pct,
                "completado": h.completado,
                "fecha_completado": h.fecha_completado,
            }
            for h in hitos
        ),
        key=lambda x: _orden_hito(x["codigo"]),
    )

    operador = proyecto.operador_red
    or_nombre = None
    if operador is not None:
        or_nombre = operador.nombre_comercial or operador.nombre_legal

    return {
        "id": registro.id,
        "proyecto_id": registro.proyecto_id,
        "nombre_comercial": proyecto.nombre_comercial,
        "codigo_cnd": proyecto.codigo_cnd,
        "clasificacion_regulatoria": _enum_val(proyecto.clasificacion_regulatoria),
        "tecnologia": _enum_val(proyecto.tipo_tecnologia),
        "operador_red": or_nombre,
        "fecha_entrada_operacion": proyecto.fecha_entrada_operacion,
        "numero_expediente": registro.numero_expediente,
        "id_requerimiento_or": registro.id_requerimiento_or,
        "numero_solicitud_appweb": registro.numero_solicitud_appweb,
        "fecha_conexion_estimada": registro.fecha_conexion_estimada,
        "vigencia_aprobacion_conexion": registro.vigencia_aprobacion_conexion,
        "fecha_visita_protecciones": registro.fecha_visita_protecciones,
        "tipo_visita_protecciones": registro.tipo_visita_protecciones,
        "exporta": registro.exporta,
        "comercializador_es_or": registro.comercializador_es_or,
        "punto_conexion_texto": registro.punto_conexion_texto,
        "notas": registro.notas,
        "avance_pct": calcular_avance_pct(hitos_av),
        "total_pct": suma_pesos(hitos_av),
        "por_etapa": por_etapa,
        "hitos": hitos_out,
        "siguiente_paso": siguiente_paso,
        "bloqueos": bloqueos,
        "alertas_pendientes": [
            {"tipo": a.tipo, "mensaje": a.mensaje, "fecha_disparo": a.fecha_disparo}
            for a in registro.alertas.all()
            if a.estado == "PENDIENTE"
        ],
    }


def _orden_hito(codigo: str):
    """Orden numerico-alfabetico de "1a".."8c"."""
    num = "".join(c for c in codigo if c.isdigit())
    letra = "".join(c for c in codigo if c.isalpha())
    return (int(num) if num else 0, letra)


def resumen_ligero(registro: RegistroConexion) -> dict:
    """Resumen reducido para la lista (evita serializar todos los hitos)."""
    r = construir_resumen(registro)
    return {
        "id": r["id"],
        "proyecto_id": r["proyecto_id"],
        "nombre_comercial": r["nombre_comercial"],
        "codigo_cnd": r["codigo_cnd"],
        "clasificacion_regulatoria": r["clasificacion_regulatoria"],
        "tecnologia": r["tecnologia"],
        "operador_red": r["operador_red"],
        "avance_pct": r["avance_pct"],
        "siguiente_paso": r["siguiente_paso"],
        "alertas_pendientes": len(r["alertas_pendientes"]),
        "bloqueos": len(r["bloqueos"]),
        "tiene_registro": True,
    }


def _fila_bare(proyecto: Proyecto) -> dict:
    """Fila de lista para un Proyecto que aun NO tiene registro de conexion:
    avance 0, todos los hitos pendientes, siguiente paso = 1a."""
    operador = proyecto.operador_red
    or_nombre = (operador.nombre_comercial or operador.nombre_legal) if operador else None
    h0 = HITOS[0]  # "1a"
    return {
        "id": None,  # aun no hay registro
        "proyecto_id": proyecto.id,
        "nombre_comercial": proyecto.nombre_comercial,
        "codigo_cnd": proyecto.codigo_cnd,
        "clasificacion_regulatoria": _enum_val(proyecto.clasificacion_regulatoria),
        "tecnologia": _enum_val(proyecto.tipo_tecnologia),
        "operador_red": or_nombre,
        "avance_pct": 0.0,
        "siguiente_paso": {
            "hito": h0["key"],
            "codigo": h0["key"],
            "descripcion": h0["descripcion"],
            "etapa": h0["etapa"],
            "etiqueta_etapa": ETIQUETAS_ETAPA[h0["etapa"]],
            "responsable": "PROMOTOR",
        },
        "alertas_pendientes": 0,
        "bloqueos": 0,
        "tiene_registro": False,
    }


def listar_todos() -> list[dict]:
    """Una fila por CADA proyecto de la plataforma (no solo los que ya tienen registro).
    Los que ya tienen registro muestran su avance real; el resto, 0% / pendiente."""
    registros = {r.proyecto_id: r for r in con_relaciones()}
    proyectos = (
        Proyecto.objects
        # El proceso de conexion/frontera aplica solo a proyectos que Unergy
        # representa: contrato de representacion vigente, en cualquier estado del
        # proyecto (el proceso empieza antes de operar). Excluye autoconsumo y demas.
        .filter(plantas.filtro_proyectos_con("representacion", hoy_col()), deleted_at__isnull=True)
        .select_related("operador_red")
        .order_by("nombre_comercial")
    )
    return [
        resumen_ligero(registros[p.id]) if p.id in registros else _fila_bare(p)
        for p in proyectos
    ]


# ---------------------------------------------------------------------------
# Transiciones
# ---------------------------------------------------------------------------
def registrar_transicion(
    registro: RegistroConexion,
    etapa: str,
    a_estado: str,
    nota: str | None = None,
    actor: str | None = None,
) -> RegistroEtapa:
    """Valida y persiste una transicion de estado; completa los hitos asociados.

    Lanza state_machine.TransicionInvalidaError si la transicion no es valida.
    """
    row = registro.etapas.filter(etapa=etapa).first()
    if row is None:
        raise ValueError(f"El registro no tiene la etapa {etapa}")

    de_estado = row.estado_actual
    if not sm.es_transicion_valida(etapa, de_estado, a_estado):
        raise sm.TransicionInvalidaError(etapa, de_estado, a_estado)

    ahora = timezone.now()
    with transaction.atomic():
        # Historial
        RegistroTransicion.objects.create(
            etapa_id=row.id, de_estado=de_estado, a_estado=a_estado, actor=actor, nota=nota,
        )

        # Actualizar la etapa
        row.estado_actual = a_estado
        row.fecha_estado = ahora
        row.responsable_actual = sm.responsable_de_estado(etapa, a_estado)
        row.bloqueada = a_estado == Estado.BLOQUEADO
        if a_estado != Estado.BLOQUEADO:
            row.causa_bloqueo = None
        row.save()

        # Completar hitos al entrar en el estado
        hitos_a_completar = list(sm.hitos_completados_al_entrar(etapa, a_estado))
        if hitos_a_completar:
            registro.hitos.filter(hito__in=hitos_a_completar, completado=False).update(
                completado=True, fecha_completado=ahora,
            )
    return row


# ---------------------------------------------------------------------------
# Alertas
# ---------------------------------------------------------------------------
def _snapshot(registro: RegistroConexion) -> alertas_mod.ProyectoSnapshot:
    def _d(v):
        if v is None:
            return None
        return v.date() if isinstance(v, datetime) else v

    equipos = list(registro.equipos.all())
    return alertas_mod.ProyectoSnapshot(
        id=registro.id,
        nombre_comercial=registro.proyecto.nombre_comercial,
        fecha_conexion_estimada=_d(registro.fecha_conexion_estimada),
        vigencia_conexion=_d(registro.vigencia_aprobacion_conexion),
        equipos_solicitados=any(e.fecha_solicitud_solenium is not None for e in equipos),
        exporta=registro.exporta,
        comercializador_es_or=registro.comercializador_es_or,
        fecha_visita_protecciones=_d(registro.fecha_visita_protecciones),
        etapas=[
            alertas_mod.EtapaSnapshot(etapa=e.etapa, estado_actual=e.estado_actual, fecha_estado=_d(e.fecha_estado))
            for e in registro.etapas.all()
        ],
        equipos=[
            alertas_mod.EquipoSnapshot(
                tipo=eq.tipo, serial=eq.serial,
                fecha_vencimiento_calibracion=_d(eq.fecha_vencimiento_calibracion),
                fecha_envio_or=_d(eq.fecha_envio_or),
            )
            for eq in equipos
        ],
    )


def recomputar_alertas(registro: RegistroConexion, hoy: date | None = None) -> list[RegistroAlerta]:
    """Recalcula las alertas del registro y las persiste (upsert por dedupe_key)."""
    hoy = hoy or hoy_col()
    generadas = alertas_mod.generar_alertas(_snapshot(registro), hoy)
    existentes = {a.dedupe_key for a in registro.alertas.all()}
    nuevas = [
        RegistroAlerta(
            registro_id=registro.id,
            tipo=g.tipo,
            fecha_disparo=datetime.combine(g.fecha_disparo, datetime.min.time()),
            estado="PENDIENTE",
            mensaje=g.mensaje,
            dedupe_key=g.dedupe_key,
        )
        for g in generadas
        if g.dedupe_key not in existentes  # ya existe (evita duplicados)
    ]
    if nuevas:
        RegistroAlerta.objects.bulk_create(nuevas)
    return nuevas
