"""SLA **contractual** del Anexo 4 del contrato de O&M, en días.

**No es el SLA operativo.** Ese vive en `dominio.py`, va en HORAS y su umbral
sale de la PRIORIDAD de la falla (8 / 24 / 72 / 168). Este va en DÍAS y su umbral
sale de la CATEGORÍA. Son dos compromisos distintos que coexisten a propósito: el
operativo es la meta interna de atención, este es el plazo de revisión que el
contrato le promete al cliente, y es el que se imprime en el informe de FMO.

Vivía en `InformesMensualesPanel.vue`, calculado en el navegador. Se movió acá el
2026-09-08 por la misma razón que el reloj operativo: un cálculo que decide lo
que ve el cliente no debe estar copiado en una vista, donde nadie lo cubre y
nadie más lo puede reusar.
"""

from __future__ import annotations

from apps.monitoreo.models import Falla

from . import dominio

# Plazo de revisión en días, con la etiqueta que imprime el Anexo 4.
CRITICO = (2, "Crítico (≥90%)")
GRAVE = (3, "Grave (66-90%)")
MEDIO = (4, "Medio (<66%)")

# `red` (desconexión de suministro) es el caso crítico y también el default:
# ante una categoría que no conozcamos, se asume el plazo más corto.
_POR_CATEGORIA = {
    "red": CRITICO,
    "frontera": GRAVE,
    "inversores": GRAVE,
    "generando_sin_datos": GRAVE,
    "eventos_adversos": MEDIO,
}

# Fallas legacy sin `clasificacion`: se cae al primer dígito de `tipo.codigo`,
# que es como se clasificaban antes del reporte estructurado.
_POR_PREFIJO_TIPO = {"1": GRAVE, "4": MEDIO, "5": MEDIO}


def plazo(falla: Falla) -> tuple[int, str]:
    """`(días de plazo, etiqueta de gravedad)` según la categoría de la falla."""
    clasificacion = falla.clasificacion if isinstance(falla.clasificacion, dict) else None
    categoria = (clasificacion or {}).get("categoria")
    if categoria:
        return _POR_CATEGORIA.get(categoria, CRITICO)

    codigo = (falla.tipo.codigo if falla.tipo_id else "") or ""
    return _POR_PREFIJO_TIPO.get(codigo[:1], CRITICO)


def evaluar(falla: Falla) -> dict:
    """`{dias, plazo_dias, etiqueta, cumple}` para la tabla del Anexo 4.

    `dias` sale de `dominio.dias_abierta`, que **mira `fecha_resolucion`**: para
    una falla cerrada son los días que estuvo abierta, no los transcurridos hasta
    hoy. El frontend lo calculaba con `Date.now()` y por eso una falla cerrada en
    un día pero identificada tres meses atrás imprimía "90d" en la columna
    "DÍAS ABIERTA" del informe que se le manda al cliente.

    **Las fallas cerradas TAMBIÉN se evalúan** (decisión del 2026-09-08). Antes
    `cumple` era `True` para toda falla en estado final, aunque se hubiera
    cerrado tarde, y el Anexo 4 quedaba estructuralmente incapaz de reportar un
    incumplimiento. Y es al revés: en una falla cerrada el cumplimiento es un
    hecho **establecido** —se sabe cuánto tardó—, mientras que en una abierta
    todavía es provisional. Ojo con eso al leer `cumple`: en una falla abierta
    significa "por ahora va dentro del plazo", no "cumplió".

    **`cumple` es `None` cuando no se puede juzgar**, y hay un caso real: una
    falla en estado final **sin `fecha_resolucion`**. Es dato legacy que existe
    en producción (lo documenta `consultas.py` en el filtro `activa_en_fecha`), y
    para esas `dias_abierta` cae a HOY, así que darían un incumplimiento enorme e
    inventado. No sabemos cuándo se cerraron: se dice que no se sabe. La regla
    anterior tapaba este caso; al evaluar las cerradas queda a la vista.
    """
    plazo_dias, etiqueta = plazo(falla)
    sin_juicio = {
        "dias": None, "plazo_dias": plazo_dias, "etiqueta": etiqueta, "cumple": None,
    }

    if not falla.fecha_identificacion:
        return sin_juicio

    es_final = bool(falla.estado_id and falla.estado.es_estado_final)
    if es_final and not falla.fecha_resolucion:
        return sin_juicio

    dias = dominio.dias_abierta(falla) or 0
    return {
        "dias": dias,
        "plazo_dias": plazo_dias,
        "etiqueta": etiqueta,
        "cumple": dias <= plazo_dias,
    }
