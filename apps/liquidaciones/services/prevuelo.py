"""Revisar que un paso del ciclo tenga sus insumos antes de dispararlo.

El 2026-09-24 se corrieron Liquidar (dos veces) y Repartir sobre una versión de
reliquidación y los tres terminaron en FAILURE. No había nada roto: esa versión
no tenía archivos del FTP ni facturas de XM. Todo lo de agosto —1.736 filas de
FTP, 2.263 de liquidación, 4 facturas— vivía en `txf`.

La plataforma tenía cómo saberlo y no lo miró: lanzó la tarea igual y dejó un
FAILURE opaco en una lista de tareas, sin decir qué faltaba.

Esto no valida la forma de la petición —eso ya lo hacen los serializers—: mira
si el trabajo tiene con qué correr, y si no, lo dice en español y antes de gastar
una tarea.

Las reglas salen de la guía de integración: Liquidar (§4.5) necesita los archivos
del FTP de esa versión; Repartir (§4.6) necesita
`readiness.ready_for_distribution`, y la propia API explica en `blockers` por qué
no lo está.

Las funciones son puras a propósito: reciben lo ya consultado. Quien hace la
consulta es la vista, que es la que sabe hablar con la API.
"""
from __future__ import annotations

from typing import Any


def problemas_para_liquidar(
    filas_ftp: int,
    version: str,
    filas_ftp_inicial: int | None = None,
) -> list[str]:
    """Qué le falta a Liquidar. Lista vacía = se puede correr.

    ``filas_ftp_inicial`` es cuántas filas hay en la versión de arranque
    (``txf``). Solo sirve para afinar el consejo: si el mes tiene datos en
    ``txf`` pero no en la versión pedida, lo que falta es el paso de
    reliquidación; si no hay en ninguna, lo que falta es descargar el FTP.
    """
    if filas_ftp:
        return []

    aviso = (
        f"No hay archivos del FTP de XM para este período en la versión "
        f"«{version}»: liquidar no tiene con qué calcular."
    )
    if filas_ftp_inicial:
        aviso += (
            f" El período sí tiene datos en la versión inicial "
            f"({filas_ftp_inicial} registros). Para reliquidar, primero usa "
            f"«Reliquidar» para copiar las facturas y después vuelve a "
            f"descargar el FTP en «{version}»."
        )
    else:
        aviso += " Descarga el FTP de XM antes de liquidar."
    return [aviso]


def problemas_para_repartir(
    readiness: dict[str, Any] | None,
    version: str,
) -> list[str]:
    """Qué le falta a Repartir, según el `readiness` que devuelve la API.

    Si la API no manda `readiness` no se inventa un problema: se deja correr y
    que falle allá con su propio error. Suponer aquí sería peor que no revisar.
    """
    if not readiness:
        return []
    if readiness.get("ready_for_distribution"):
        return []

    # Los bloqueos vienen redactados por la API y son precisos ("falta la
    # factura de comercializador procesada"). Se muestran tal cual: reescribirlos
    # solo los alejaría de lo que de verdad pasa.
    bloqueos = [str(b) for b in (readiness.get("blockers") or []) if b]
    if bloqueos:
        return bloqueos

    return [
        f"Las facturas de XM de este período y versión («{version}») no están "
        f"listas para repartir, y la API no dijo por qué."
    ]
