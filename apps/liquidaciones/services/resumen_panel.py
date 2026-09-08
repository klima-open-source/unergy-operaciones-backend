"""El resumen de Liquidaciones = espejo de lectura del Panel Contable.

Los tres tabs de la pantalla (Resumen, Proyectos, Inversionistas) se arman de
aquí, así que **cuadran siempre con el Panel**. La tabla `liquidaciones` queda
solo para el detalle operativo —mandatos, facturas, XM—; por eso cada proyecto
trae su `liquidacion_id` para poder navegar al detalle.

`construir` es una función PURA: recibe los paneles y los mapas de apoyo ya
cargados y no toca la base. Eso la hace testeable sola, que es como estaba
pensada en el router original.
"""

from apps.liquidaciones.services.impuestos import (
    impuestos_de_factura, tasas_efectivas,
)


def construir(paneles, periodo_norm, tipo, nombres, tipos,
              liq_por_proyecto, cliente_por_pi, overrides=None) -> dict:
    """Arma el resumen espejo desde paneles ya cargados y sus mapas de apoyo.

    Función pura: sin acceso a base, testeable sola. Los mapas son nombres y
    tipos de proyecto, `liquidacion_id` por proyecto y cliente por
    `proyecto_inversionista`.
    """
    overrides = overrides or {}
    proyectos_out = []
    total_valor_a_pagar = 0.0
    total_ingresos = 0.0
    total_costos = 0.0

    for panel in paneles:
        inv_map: dict = {}
        for ln in sorted(panel.lineas.all(), key=lambda x: x.orden):
            key = ln.proyecto_inversionista_id or f"_{ln.inversionista_nombre}"
            if key not in inv_map:
                cli = cliente_por_pi.get(ln.proyecto_inversionista_id) or {}
                inv_map[key] = {
                    "proyecto_inversionista_id": ln.proyecto_inversionista_id,
                    "cliente_id": cli.get("cliente_id"),
                    "cliente_nombre": cli.get("cliente_nombre") or ln.inversionista_nombre,
                    "nombre": ln.inversionista_nombre,
                    "porcentaje": float(ln.porcentaje) if ln.porcentaje is not None else None,
                    "rates": cli,
                    "grupos": {},
                    "conceptos": [],
                }
            valor = float(ln.valor_cop) if ln.valor_cop is not None else 0.0
            inv_map[key]["grupos"][ln.grupo] = inv_map[key]["grupos"].get(ln.grupo, 0.0) + valor
            inv_map[key]["conceptos"].append({
                "grupo": ln.grupo, "concepto": ln.concepto, "valor_cop": valor,
                # Trazabilidad (auditoría): comprobante contable y celda de origen del ER.
                "comprobante_contable": ln.comprobante_contable,
                "origen": f"{ln.hoja}!{ln.celda}" if (ln.hoja and ln.celda) else None,
            })
            # Desglose de impuestos de la factura de servicio (Rep/CGM/Admin) en
            # tiempo de lectura, con las tasas del cliente (+ excepción por servicio/
            # proyecto). base + IVA − retenciones.
            _eff = tasas_efectivas(
                inv_map[key]["rates"],
                overrides.get((inv_map[key]["cliente_id"], ln.concepto)),
                panel.proyecto_id,
            )
            for imp in impuestos_de_factura(ln.concepto, valor, _eff):
                inv_map[key]["grupos"]["facturas"] = inv_map[key]["grupos"].get("facturas", 0.0) + imp["valor"]
                inv_map[key]["conceptos"].append({
                    "grupo": "facturas", "concepto": imp["concepto"], "valor_cop": imp["valor"],
                    "comprobante_contable": None, "origen": None,
                })

        inversionistas_out = []
        proyecto_valor_a_pagar = 0.0
        proyecto_ingresos = 0.0
        proyecto_costos = 0.0
        for inv in inv_map.values():
            grupos = inv["grupos"]
            # El Panel guarda comercializacion/costos/facturas con signo NEGATIVO, así
            # que el valor a pagar es la SUMA de todas las líneas con su signo — idéntico
            # a utilidad(inv) del Panel Contable (PanelContableView.vue:625). Si existe un
            # grupo 'resultado' explícito, ese ya es el neto y no se re-suma.
            if "resultado" in grupos:
                valor_a_pagar = grupos["resultado"]
            else:
                valor_a_pagar = sum(grupos.values())
            inversionistas_out.append({
                "proyecto_inversionista_id": inv["proyecto_inversionista_id"],
                "cliente_id": inv["cliente_id"],
                "cliente_nombre": inv["cliente_nombre"],
                "nombre": inv["nombre"],
                "porcentaje": inv["porcentaje"],
                "valor_a_pagar": round(valor_a_pagar, 2),
                "grupos_totales": {g: round(v, 2) for g, v in grupos.items()},
                "conceptos": inv["conceptos"],
            })
            proyecto_valor_a_pagar += valor_a_pagar
            proyecto_ingresos += grupos.get("ingresos", 0.0)
            # Costos = todo lo que resta (comercializacion + costos + facturas), con signo.
            proyecto_costos += (
                grupos.get("comercializacion", 0.0)
                + grupos.get("costos", 0.0)
                + grupos.get("facturas", 0.0)
            )

        proyectos_out.append({
            "panel_id": panel.id,
            "proyecto_id": panel.proyecto_id,
            "proyecto": nombres.get(panel.proyecto_id, f"Proyecto {panel.proyecto_id}"),
            "tipo_proyecto": tipos.get(panel.proyecto_id),
            "liquidacion_id": liq_por_proyecto.get(panel.proyecto_id),
            "consecutivo_ingresos": panel.consecutivo_ingresos,
            "consecutivo_costos": panel.consecutivo_costos,
            "fecha_firma": panel.fecha_firma.isoformat() if panel.fecha_firma else None,
            "liquidar_ingresos": panel.liquidar_ingresos,
            "liquidar_costos": panel.liquidar_costos,
            "estado": "firmado" if panel.fecha_firma else "pendiente",
            "valor_a_pagar_total": round(proyecto_valor_a_pagar, 2),
            "ingresos_cop": round(proyecto_ingresos, 2),
            "costos_cop": round(proyecto_costos, 2),
            "inversionistas": inversionistas_out,
        })
        total_valor_a_pagar += proyecto_valor_a_pagar
        total_ingresos += proyecto_ingresos
        total_costos += proyecto_costos

    return {
        "periodo": periodo_norm,
        "tipo": tipo,
        "resumen": {
            "num_proyectos": len(proyectos_out),
            "valor_a_pagar_total": round(total_valor_a_pagar, 2),
            "ingresos_total_cop": round(total_ingresos, 2),
            "costos_total_cop": round(total_costos, 2),
            # costos ya viene con signo negativo → neto = ingresos + costos.
            "ingreso_neto_cop": round(total_ingresos + total_costos, 2),
        },
        "proyectos": proyectos_out,
    }
