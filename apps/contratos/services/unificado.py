"""Forma común de un servicio: lo mismo para un contrato de servicio y un PPA.

Deja ver juntos dos cosas que viven en tablas distintas —`contratos_servicio` y
`ppa_contratos`— sin fusionarlas. Ver `docs/SERVICIOS_AGRUPACION.md` §7.

**Qué es común y qué no.** Va acá SOLO lo que sirve para agrupar, contar o
filtrar de forma transversal: grupo, subservicios, plantas, vigencia, semáforo,
enlace. Lo específico de cada tipo de contrato se queda con su nombre:

    fuente = "ppa"       → partes = {comprador, vendedor}
    fuente = "servicio"  → partes = {contratante, prestador}

Uniformarlas a una "contraparte" genérica borraría la diferencia: en un PPA
dicen quién compra energía y quién la vende; en un contrato de servicio, quién
paga el servicio y quién lo presta. No son sinónimos. El front ya los trata
aparte (`FiltrosPpa` vs `FiltrosServicio` en `filtrosServicios.ts`).

**`plantas` es lista** porque un PPA puede cubrir varias y un contrato de
servicio siempre cubre una. De ahí la advertencia de §4-bis: los totales se
calculan por contrato, nunca sumando por planta, o un PPA de 5 plantas se
cuenta cinco veces.
"""

from __future__ import annotations

from datetime import date

from apps.clientes.services.panel import semaforo_contrato
from apps.contratos.services import grupos

FUENTE_SERVICIO = "servicio"
FUENTE_PPA = "ppa"


def _fecha(valor: date | None) -> str | None:
    return valor.isoformat() if valor else None


def _planta(proyecto) -> dict:
    return {
        "proyecto_id": proyecto.id,
        "nombre": proyecto.nombre_comercial,
        "tipo_proyecto": proyecto.tipo_proyecto,
    }


def desde_contrato_servicio(contrato, hoy: date, proyecto=None,
                            enlace: str | None = None) -> dict:
    """La forma común de un `ContratoServicio`.

    `proyecto` y `enlace` se pasan ya resueltos: quien llama los trae precargados
    para todas las filas de una vez. Buscarlos acá sería un N+1 -- el mismo
    motivo por el que `api/v1/contratos_servicio/queryset.py` precarga.

    Un contrato `terminado` sale como `vencido` aunque su fecha fin no haya
    pasado: es la regla que ya aplica `apps/clientes/services/vistas.py`.
    """
    subservicios = grupos.subservicios_de(contrato)
    return {
        "fuente": FUENTE_SERVICIO,
        "contrato_id": contrato.id,
        "grupo": grupos.grupo_de_contrato(contrato),
        "subservicios": subservicios,
        "numero": contrato.numero_contrato,
        "plantas": [_planta(proyecto)] if proyecto else [],
        "fecha_inicio": _fecha(contrato.fecha_inicio),
        "fecha_fin": _fecha(contrato.fecha_fin),
        "estado": contrato.estado,
        "semaforo": "vencido" if contrato.estado == "terminado"
        else semaforo_contrato(contrato.fecha_fin, hoy),
        "renovacion_automatica": contrato.renovacion_automatica,
        "tarifas": grupos.tarifas_de(contrato),
        "partes": {
            "contratante": contrato.contratante_nombre,
            "prestador": contrato.prestador_nombre,
        },
        "enlace": enlace,
    }


def desde_ppa(contrato_ppa, hoy: date, proyectos=(),
              enlace: str | None = None) -> dict:
    """La forma común de un `PpaContrato`.

    `estado` va en `None` a propósito: **`ppa_contratos` no tiene esa columna**.
    Su vigencia se deriva de las fechas, y por eso el front calcula `_vigencia`
    aparte (`ppaVigencia.ts`). Inventar un estado acá sería fabricar un dato.

    `tarifas` va vacío por la misma razón de §4-bis: `ppa_tarifas` cuelga del
    contrato y cambia mes a mes, y no existe el reparto por planta. Quien
    necesite la tarifa de un PPA la pide a su propio endpoint.
    """
    return {
        "fuente": FUENTE_PPA,
        "contrato_id": contrato_ppa.id,
        "grupo": grupos.PPA,
        "subservicios": [grupos.subservicio_de_ppa(contrato_ppa)],
        "numero": contrato_ppa.numero_codigo_contrato or contrato_ppa.nombre_interno,
        "plantas": [_planta(p) for p in proyectos],
        "fecha_inicio": _fecha(contrato_ppa.fecha_inicio),
        "fecha_fin": _fecha(contrato_ppa.fecha_fin),
        "estado": None,
        "semaforo": semaforo_contrato(contrato_ppa.fecha_fin, hoy),
        "renovacion_automatica": contrato_ppa.renovacion_automatica,
        "tarifas": {},
        "partes": {
            "comprador": contrato_ppa.comprador_nombre,
            "vendedor": contrato_ppa.vendedor_nombre,
        },
        "enlace": enlace,
    }


def contar(servicios: list[dict]) -> dict[str, int]:
    """Cuántos contratos y cuántas plantas cubre un conjunto de servicios.

    **No son la misma cifra y por eso van separadas** (§4-bis): un PPA de 5
    plantas cuenta 1 como contrato y 5 como plantas. Las plantas se deduplican
    entre contratos -- una planta con mantenimiento, arriendo e internet son
    tres contratos sobre UNA planta.
    """
    plantas: set[int] = set()
    for servicio in servicios:
        plantas.update(p["proyecto_id"] for p in servicio["plantas"])
    return {"contratos": len(servicios), "plantas": len(plantas)}


def agrupar(servicios: list[dict]) -> list[dict]:
    """Los servicios repartidos en los tres grupos, en el orden del catálogo.

    Un grupo sin servicios igual sale, con sus conteos en cero: la vista tiene
    tres pestañas fijas y una que desaparece se lee como un error de carga.
    """
    from apps.clientes.services.panel import peor_semaforo

    por_grupo: dict[str, list[dict]] = {g: [] for g in grupos.ORDEN_GRUPOS}
    for servicio in servicios:
        if servicio["grupo"] in por_grupo:
            por_grupo[servicio["grupo"]].append(servicio)

    salida = []
    for grupo in grupos.ORDEN_GRUPOS:
        filas = por_grupo[grupo]
        # Los que vencen antes, primero; los indefinidos al final.
        filas.sort(key=lambda s: (s["fecha_fin"] is None, s["fecha_fin"] or ""))
        salida.append({
            "grupo": grupo,
            "subservicios": list(grupos.SUBSERVICIOS[grupo]),
            "conteos": contar(filas),
            "semaforo": peor_semaforo([s["semaforo"] for s in filas]),
            "servicios": filas,
        })
    return salida
