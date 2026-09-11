"""Lo que debe entrar por proyecto contra lo que ya se liquidó en despachos.

Dos cifras del mismo mes que hasta ahora no se cruzaban en ninguna pantalla:

  * **Debe ingresar** lo calcula Facturación en esta base, contrato por
    contrato: kWh del despacho de XM × tarifa PPA indexada, más la energía sin
    PPA valorizada al precio de bolsa del mes.
  * **Liquidado en despachos** sale de la API de Liquidaciones
    (``market_settlements``), que es el dato crudo que produce «Liquidar».

La definición de lo liquidado la fijó Jessica y **no** es la del Panel Contable:

  * suma lo vendido: ``dispatch`` (contrato) y ``dispatch_fazni``, que es como
    la API marca la **venta en bolsa**;
  * y **no resta las compras en bolsa**. El Panel sí las resta —allá el ingreso
    bruto es neto de lo que salió—; acá la pregunta es cuánto se liquidó a favor
    del proyecto, y una compra no lo disminuye. Las compras se devuelven aparte,
    en ``compras_bolsa``, para que se vean sin afectar el total.

Es lógica pura, sin BD ni HTTP: las dos listas se las pasa la vista.
"""
from __future__ import annotations

from typing import Any

# `dispatch_fazni` es la venta en bolsa. Mismos tipos que usa el Panel
# (`apps/contabilidad/services/desde_api.py`), pero acá `purchase` no resta.
TIPOS_VENTA = ("dispatch", "dispatch_fazni")


def _num(valor: Any) -> float:
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


def _vacia(clave: Any, proyecto_id: int | None, nombre: str) -> dict[str, Any]:
    return {
        "_clave": clave,
        "proyecto_id": proyecto_id,
        "proyecto": nombre,
        "topico": None,
        "contratos_sic": [],
        # Lado Facturación
        "kwh_facturacion": 0.0,
        "debe_ingresar_ppa": 0.0,
        "ingreso_bolsa": 0.0,
        "kwh_sin_valorizar": 0.0,
        "lineas_sin_valorizar": 0,
        # Lado Liquidaciones
        "kwh_despachos": 0.0,
        "liquidado_despachos": 0.0,
        "compras_bolsa": 0.0,
    }


def _acumular_facturacion(filas: dict, lineas, bolsa) -> None:
    for linea in lineas:
        proyecto_id = linea.get("proyecto_id")
        contrato = linea.get("contrato") or "—"
        # Un SIC sin dueño no tiene proyecto: se muestra por su contrato en vez
        # de descartarlo o de mezclarlo con los demás huérfanos en una fila sola.
        clave = ("p", proyecto_id) if proyecto_id else ("sic", contrato)
        nombre = linea.get("proyecto") or f"Sin proyecto (contrato {contrato})"
        fila = filas.setdefault(clave, _vacia(clave, proyecto_id, nombre))
        if contrato not in fila["contratos_sic"]:
            fila["contratos_sic"].append(contrato)

        kwh = _num(linea.get("kwh"))
        estado = linea.get("estado")
        if estado == "ok":
            fila["kwh_facturacion"] += kwh
            fila["debe_ingresar_ppa"] += _num(linea.get("facturacion"))
        elif estado == "sin_ppa":
            # Esa energía sí se vende; se valoriza al precio de bolsa que carga
            # la usuaria cada mes. Sin precio no se inventa uno: queda sin
            # valorizar y se avisa, porque un cero se lee como «no vendió».
            fila["kwh_facturacion"] += kwh
            if bolsa:
                fila["ingreso_bolsa"] += kwh * float(bolsa)
            else:
                fila["kwh_sin_valorizar"] += kwh
                fila["lineas_sin_valorizar"] += 1
        else:
            # sin_tarifa / sin_ipp_base / sin_ipp_mes: hay energía despachada
            # pero falta un dato para ponerle precio.
            fila["kwh_sin_valorizar"] += kwh
            fila["lineas_sin_valorizar"] += 1


def _acumular_despachos(filas: dict, despachos, proyectos_por_topico) -> None:
    for despacho in despachos:
        topico = despacho.get("project") or "—"
        proyecto = (proyectos_por_topico or {}).get(topico) or {}
        proyecto_id = proyecto.get("id")
        clave = ("p", proyecto_id) if proyecto_id else ("topico", topico)
        nombre = proyecto.get("nombre") or topico
        fila = filas.setdefault(clave, _vacia(clave, proyecto_id, nombre))
        fila["topico"] = topico

        tipo = despacho.get("data_type")
        if tipo in TIPOS_VENTA:
            fila["kwh_despachos"] += _num(despacho.get("energy"))
            fila["liquidado_despachos"] += _num(despacho.get("price"))
        elif tipo == "purchase":
            # En positivo: el signo que manda la API no es confiable, y esta
            # columna es informativa —nunca se resta.
            fila["compras_bolsa"] += abs(_num(despacho.get("price")))


def comparar(
    lineas: list[dict[str, Any]],
    despachos: list[dict[str, Any]],
    bolsa: float | None = None,
    proyectos_por_topico: dict[str, dict] | None = None,
) -> list[dict[str, Any]]:
    """Una fila por proyecto con las dos cifras y su diferencia.

    ``lineas`` son las de :func:`apps.facturacion.services.calculo.periodo`;
    ``despachos``, las filas crudas de ``market_settlements``;
    ``proyectos_por_topico`` mapea el ``nombre_topico`` de la API al proyecto de
    esta base.

    Un proyecto que solo aparece de un lado **sale igual**: es justamente lo que
    este cruce busca destapar.
    """
    filas: dict[Any, dict[str, Any]] = {}
    _acumular_facturacion(filas, lineas, bolsa)
    _acumular_despachos(filas, despachos, proyectos_por_topico)

    salida = []
    for fila in filas.values():
        fila.pop("_clave", None)
        for campo in (
            "kwh_facturacion", "debe_ingresar_ppa", "ingreso_bolsa",
            "kwh_sin_valorizar", "kwh_despachos", "liquidado_despachos",
            "compras_bolsa",
        ):
            fila[campo] = round(fila[campo], 2)
        fila["debe_ingresar"] = round(
            fila["debe_ingresar_ppa"] + fila["ingreso_bolsa"], 2
        )
        fila["diferencia"] = round(
            fila["debe_ingresar"] - fila["liquidado_despachos"], 2
        )
        salida.append(fila)

    return sorted(
        salida,
        key=lambda f: (-f["debe_ingresar"], -f["liquidado_despachos"],
                       f["proyecto"] or ""),
    )


def totales(filas: list[dict[str, Any]]) -> dict[str, Any]:
    """Los totales del mes, para la franja de resumen de la vista."""
    campos = (
        "kwh_facturacion", "debe_ingresar_ppa", "ingreso_bolsa", "debe_ingresar",
        "kwh_despachos", "liquidado_despachos", "compras_bolsa", "diferencia",
    )
    resumen = {c: round(sum(f[c] for f in filas), 2) for c in campos}
    resumen["proyectos"] = len(filas)
    resumen["sin_valorizar"] = sum(f["lineas_sin_valorizar"] for f in filas)
    return resumen
