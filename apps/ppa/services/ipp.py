"""Trae el IPP del DANE desde la API de Liquidaciones a nuestro `ipp_mensual`.

El mismo número se consultaba en dos sitios que no se hablaban: la API de
Liquidaciones se lo pide al DANE para liquidar, y Facturación lo lee de
`ipp_mensual`, que se llenaba a mano. Verificado el 2026-09-10: los 8 meses que
había en ambos lados coincidían **hasta el último decimal**.

Tenerlos separados costaba caro. La API tenía **15 meses que nosotros no**:
2026-08 —el que se iba a facturar— y todo el rango oct-2024 a nov-2025, de donde
salen los `ipp_base` de los PPA. Sin esos meses, esas líneas de facturación caen
en `sin_ipp_base` y no se pueden calcular.

Se dispara **a mano** desde Liquidaciones, no por horario: el IPP se publica una
vez al mes y quien liquida prefiere decidir cuándo traerlo.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from apps.ppa.models import IppMensual

logger = logging.getLogger("operaciones.ipp")

# `valor` es DecimalField(max_digits=12, decimal_places=4). Comparar en float da
# falsos "cambió" por el último bit, así que se compara con esta tolerancia.
TOLERANCIA = Decimal("0.0001")


def _traer_de_la_api() -> list[dict[str, Any]]:
    """Aparte para poder sustituirla en las pruebas sin tocar la red."""
    from apps.liquidaciones.services import api_externa as api

    return api.listar_ipp_historico()


def elegir_por_mes(filas: list[dict[str, Any]]) -> dict[tuple[int, int], float]:
    """Un solo IPP por mes: el de la consulta al DANE más reciente.

    La API guarda **una fila por consulta**, fechada el día en que se preguntó y
    no el día 1 del mes: el 2026-09-10 traía 39 filas para 23 meses, y un mes con
    nueve. Quedarse con cualquiera sería una lotería.
    """
    mejor: dict[tuple[int, int], tuple[str, float]] = {}
    for fila in filas:
        anio, mes, valor = fila.get("year"), fila.get("month"), fila.get("ipp")
        if anio is None or mes is None or valor is None:
            continue
        clave = (int(anio), int(mes))
        # `date` puede faltar; se ordena con "" para no perder el mes.
        fecha = str(fila.get("date") or "")
        if clave not in mejor or fecha >= mejor[clave][0]:
            mejor[clave] = (fecha, float(valor))
    return {k: v for k, (_fecha, v) in mejor.items()}


def clasificar(
    de_la_api: dict[tuple[int, int], float],
    nuestros: dict[tuple[int, int], float],
) -> dict[str, Any]:
    """Qué crear, qué pisar y qué dejar como está.

    Ante un valor distinto **manda la API**: es la misma fuente que usa la
    liquidación, y si los dos lados se separan el ingreso facturado deja de
    cuadrar con el liquidado. Pero el cambio se reporta, porque mueve un mes que
    quizá ya se facturó.

    Lo que existe de nuestro lado y la API no tiene **no se toca**: esto trae, no
    borra.
    """
    crear: dict[tuple[int, int], float] = {}
    actualizar: dict[tuple[int, int], float] = {}
    cambios: list[dict[str, Any]] = []
    sin_cambio = 0

    for clave, valor in sorted(de_la_api.items()):
        actual = nuestros.get(clave)
        if actual is None:
            crear[clave] = valor
        elif abs(Decimal(str(actual)) - Decimal(str(valor))) > TOLERANCIA:
            actualizar[clave] = valor
            cambios.append({
                "periodo": f"{clave[0]}-{clave[1]:02d}",
                "antes": actual, "ahora": valor,
            })
        else:
            sin_cambio += 1

    return {"crear": crear, "actualizar": actualizar,
            "cambios": cambios, "sin_cambio": sin_cambio}


def sincronizar() -> dict[str, Any]:
    """Trae el histórico completo del IPP y lo deja en `ipp_mensual`."""
    de_la_api = elegir_por_mes(_traer_de_la_api())
    nuestros = {
        (i.año, i.mes): float(i.valor)
        for i in IppMensual.objects.all()
    }
    plan = clasificar(de_la_api, nuestros)

    if plan["crear"]:
        IppMensual.objects.bulk_create([
            IppMensual(año=a, mes=m, valor=Decimal(str(v)))
            for (a, m), v in plan["crear"].items()
        ])
    for (a, m), v in plan["actualizar"].items():
        IppMensual.objects.filter(año=a, mes=m).update(valor=Decimal(str(v)))

    if plan["cambios"]:
        logger.warning(
            "Sincronizar IPP cambió %s mes(es) que ya existían: %s",
            len(plan["cambios"]), plan["cambios"],
        )

    return {
        "creados": len(plan["crear"]),
        "actualizados": len(plan["actualizar"]),
        "sin_cambio": plan["sin_cambio"],
        "cambios": plan["cambios"],
        "periodos_creados": sorted(
            f"{a}-{m:02d}" for a, m in plan["crear"]
        ),
        "total_en_base": IppMensual.objects.count(),
    }
