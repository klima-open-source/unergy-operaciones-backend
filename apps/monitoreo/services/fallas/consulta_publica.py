"""Consulta de fallas por proyecto para consumidores externos (API Key).

Por qué existe aparte de `GET /fallas`:

  · El listado interno expone el catálogo crudo de estados (`abierta`,
    `en_gestion`, `en_espera`, `programado`, `cerrada`, `sin_solucion`), que es
    un detalle de cómo opera el equipo adentro. Quien integra desde afuera
    razona en tres cubetas: la falla está viva, está agendada, o ya se acabó.
    Acá se fija ese contrato de tres estados y se traduce a los códigos reales.
  · El listado interno devuelve correos de usuarios internos y el objeto de
    catálogo completo. La ficha pública deja fuera los correos y aplana lo que
    de verdad le sirve a un tercero.

Ya no incluye `asignado_a`: campo eliminado del modelo (auditoría 2026-09-02,
sin adopción real -- ver docs/API_FALLAS.md).

Si mañana se agrega un estado al catálogo, cae en `vigente` o en `terminado`
según `es_estado_final` — nunca queda invisible. Ver `grupo_de_estado`.
"""
from datetime import date

from apps.monitoreo.services.fallas import dominio
from apps.monitoreo.services.fallas.titulo import titulo_falla

# El único estado agendado. Todo lo demás se decide por `es_estado_final`,
# para que un estado nuevo en el catálogo nunca desaparezca de las respuestas.
ESTADO_PROGRAMADO = "programado"

GRUPO_VIGENTE = "vigente"
GRUPO_PROGRAMADO = "programado"
GRUPO_TERMINADO = "terminado"
GRUPO_TODAS = "todas"

GRUPOS = (GRUPO_VIGENTE, GRUPO_PROGRAMADO, GRUPO_TERMINADO)
GRUPOS_CONSULTABLES = (*GRUPOS, GRUPO_TODAS)

# Qué significa cada cubeta, para documentar la respuesta sin que el consumidor
# tenga que adivinar. Las etiquetas viajan en el payload.
DESCRIPCION_GRUPOS = {
    GRUPO_VIGENTE: "Falla abierta: identificada y todavía sin resolver.",
    GRUPO_PROGRAMADO: "Falla con intervención agendada (ver fecha_programada).",
    GRUPO_TERMINADO: "Falla cerrada: ya no está en operación (resuelta o sin solución).",
}


def grupo_de_estado(codigo: str | None, es_estado_final: bool | None) -> str:
    """Traduce un estado del catálogo interno a la cubeta pública.

    `programado` gana sobre todo lo demás: es un estado no-final, pero el
    consumidor lo pidió separado de "vigente" justamente porque la falla está
    identificada y con fecha de intervención, no en gestión activa.
    """
    if codigo == ESTADO_PROGRAMADO:
        return GRUPO_PROGRAMADO
    return GRUPO_TERMINADO if es_estado_final else GRUPO_VIGENTE


def codigos_de_grupo(estados: list, grupo: str) -> list[str]:
    """Códigos del catálogo que caen en una cubeta pública.

    `estados` son filas de fallas_cat_estados (necesitan .codigo y
    .es_estado_final). Se resuelve contra la BD y no contra una lista fija para
    que un estado agregado después siga clasificando bien.
    """
    return [e.codigo for e in estados
            if grupo_de_estado(e.codigo, e.es_estado_final) == grupo]


def _clasificacion_legible(falla) -> dict | None:
    """Categoría/subtipo del reporte estructurado, ya con etiquetas.

    Las fallas viejas (anteriores al reporte estructurado) no tienen
    categoria_codigo; para esas devuelve None y el consumidor cae en `tipo`.
    """
    from apps.monitoreo.services.fallas.estructura import get_categoria, etiqueta_subtipo

    if not falla.categoria_codigo:
        return None
    cat = get_categoria(falla.categoria_codigo)
    return {
        "categoria_codigo": falla.categoria_codigo,
        "categoria": cat["etiqueta"] if cat else falla.categoria_codigo,
        "subtipo_codigo": falla.subtipo_codigo,
        "subtipo": (etiqueta_subtipo(falla.categoria_codigo, falla.subtipo_codigo)
                    if falla.subtipo_codigo else None),
        "detalle": falla.subtipo_detalle,
    }


def _num(v) -> float | None:
    """Numeric de SQLAlchemy -> float de JSON (Decimal no serializa)."""
    return None if v is None else float(v)


def falla_publica(falla) -> dict:
    """Ficha de una falla tal como la ve un consumidor externo.

    Deliberadamente NO incluye correos de usuarios internos ni los ids del
    catálogo, que no son estables entre entornos: el consumidor se guía por
    `estado.codigo`/`estado.grupo`, no por números.
    """
    estado = falla.estado
    prioridad = falla.prioridad
    grupo = grupo_de_estado(
        estado.codigo if estado else None,
        estado.es_estado_final if estado else None,
    )
    return {
        "id": falla.id,
        "codigo": falla.codigo_interno,
        "estado": {
            "codigo": estado.codigo if estado else None,
            "etiqueta": estado.etiqueta if estado else None,
            "grupo": grupo,
            "es_estado_final": bool(estado.es_estado_final) if estado else None,
        },
        "prioridad": {
            "codigo": prioridad.codigo if prioridad else None,
            "etiqueta": prioridad.etiqueta if prioridad else None,
            "nivel": prioridad.nivel if prioridad else None,
        },
        "descripcion": falla.descripcion,
        "tipo": titulo_falla(falla),
        "clasificacion": _clasificacion_legible(falla),
        "resolucion": (falla.resolucion.etiqueta if falla.resolucion else None),
        "causa_raiz": falla.causa_raiz,
        "acciones_correctivas": falla.acciones_correctivas,
        "fecha_identificacion": falla.fecha_identificacion,
        "hora_identificacion": falla.hora_identificacion,
        "fecha_ocurrencia": falla.fecha_ocurrencia,
        "fecha_programada": falla.fecha_programada,
        "fecha_resolucion": falla.fecha_resolucion,
        "dias_abierta": dominio.dias_abierta(falla),
        "tiempo_afectacion_horas": dominio.tiempo_afectacion_horas(falla),
        "sla_limite_horas": falla.sla_limite_horas,
        "sla_cumplido": falla.sla_cumplido,
        "kwh_perdidos_estimado": _num(falla.kwh_perdidos_estimado),
        "impacto_economico_cop": _num(falla.impacto_economico_cop),
        "frontera_afecta_medicion": falla.frontera_afecta_medicion,
        "frontera_perdida_comunicacion": falla.frontera_perdida_comunicacion,
        "inversores_perdida_comunicacion": falla.inversores_perdida_comunicacion,
        "creada_en": falla.created_at,
        "actualizada_en": falla.updated_at,
    }


def proyecto_publico(proyecto) -> dict:
    """Identidad del proyecto, con las llaves que sirven para cruzarlo afuera."""
    return {
        "id": proyecto.id,
        "nombre": proyecto.nombre_comercial,
        # Misma llave que publica /comercial/proyectos-operando: es como se
        # identifica la planta en la API de generación de Unergy.
        "api_id_unergy": proyecto.sub_project,
        "sub_project": proyecto.sub_project,
        "estado": getattr(proyecto.estado, "value", proyecto.estado),
        "municipio": proyecto.municipio,
        "departamento": proyecto.departamento,
        "potencia_instalada_kwp": _num(proyecto.potencia_instalada_kwp),
    }
