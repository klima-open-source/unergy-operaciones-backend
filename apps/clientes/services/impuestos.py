"""
Desglose de impuestos de las facturas de servicio (Representación, CGM,
Administración) en tiempo de LECTURA — no se guardan en el panel.

Lo usan liquidaciones y contabilidad (antes cada una tenía su copia idéntica).

**De dónde sale cada %** (`tasas_efectivas`): la tasa general del cliente
(`clientes.iva_pct`, `retencion_pct`, `reteiva_pct`, `reteica_pct`), salvo que haya
una excepción en `cliente_tasa_servicio` para ese cliente y ese servicio --en una
planta o en todas--. No es una copia de las tasas del cliente: es la excepción a
ellas. Precedencia: excepción de la planta > excepción del servicio > tasa general,
y un % vacío en la excepción hereda el general. El caso real (2026-10-14): Solenium
retiene 4 % en general y 11 % en Administración (la retención depende del concepto).

La base (Rep/CGM/Admin) sale del ER y en el panel está con signo NEGATIVO
(reduce el "valor a pagar"). Sobre esa base se generan, por inversionista, las
líneas de impuesto usando las tasas del CLIENTE:

    Iva {Servicio}      = base × iva%          (mismo signo que la base → negativo)
    ReteFuente {Ab} X%  = −base × retencion%   (signo opuesto → positivo, reduce deducción)
    ReteIVA {Ab} X%     = −base × reteiva%      (positivo)
    ReteICA {Ab} X%     = −base × reteica%      (positivo)

Efecto neto en valor a pagar = base + IVA − retenciones (confirmado con la usuaria
2026-07-15). Como valor a pagar = Σ líneas con signo, basta sumar estas líneas.
"""



SERVICIOS_FACTURA = ("Representación", "CGM", "Administración")
_ABBR = {"Representación": "Rep", "CGM": "CGM", "Administración": "Adm"}
RATE_KEYS = ("iva_pct", "retencion_pct", "reteiva_pct", "reteica_pct")


def overrides_tasa_servicio(cliente_ids) -> dict:
    """`{(cliente_id, servicio): {proyecto_id o None: {tasas}}}` desde
    `cliente_tasa_servicio`."""
    ids = {c for c in (cliente_ids or set()) if c}
    out: dict = {}
    if not ids:
        return out
    from apps.clientes import models as cl_models

    for row in cl_models.ClienteTasaServicio.objects.filter(cliente_id__in=ids):
        out.setdefault((row.cliente_id, row.servicio), {})[row.proyecto_id] = {
            "iva_pct": float(row.iva_pct) if row.iva_pct is not None else None,
            "retencion_pct": float(row.retencion_pct) if row.retencion_pct is not None else None,
            "reteiva_pct": float(row.reteiva_pct) if row.reteiva_pct is not None else None,
            "reteica_pct": float(row.reteica_pct) if row.reteica_pct is not None else None,
        }
    return out


def tasas_efectivas(base_rates: dict | None, overrides_serv: dict | None,
                    proyecto_id) -> dict:
    """Combina las tasas generales del cliente (base_rates) con la excepción por
    servicio (overrides_serv). overrides_serv: {proyecto_id_or_None: {rates}} para
    un (cliente, servicio). Precedencia: excepción del proyecto > excepción global
    (proyecto_id None) > tasa general. Un _pct null en la excepción hereda la general.
    """
    base_rates = base_rates or {}
    spec = {}
    if overrides_serv:
        spec = overrides_serv.get(proyecto_id) or overrides_serv.get(None) or {}
    return {k: (spec[k] if spec.get(k) is not None else base_rates.get(k)) for k in RATE_KEYS}


def _pct(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def impuestos_de_factura(concepto: str, valor_base, rates: dict | None) -> list[dict]:
    """Líneas de impuesto para una factura de servicio ya dividida por inversionista.

    concepto: "Representación" | "CGM" | "Administración".
    valor_base: valor de la línea base (con signo; negativo en el panel).
    rates: {iva_pct, retencion_pct, reteiva_pct, reteica_pct} del cliente (en %).
    Devuelve [] si no es una factura de servicio o no hay tasas.
    """
    if concepto not in SERVICIOS_FACTURA or not rates:
        return []
    base = float(valor_base or 0.0)
    if base == 0.0:
        return []
    ab = _ABBR.get(concepto, concepto)
    iva = _pct(rates.get("iva_pct"))
    ret = _pct(rates.get("retencion_pct"))
    rei = _pct(rates.get("reteiva_pct"))
    ica = _pct(rates.get("reteica_pct"))
    out: list[dict] = []
    if iva:
        out.append({"concepto": f"Iva {concepto}", "valor": round(base * iva / 100.0, 2)})
    if ret:
        out.append({"concepto": f"ReteFuente {ab} {ret:g}%", "valor": round(-base * ret / 100.0, 2)})
    if rei:
        out.append({"concepto": f"ReteIVA {ab} {rei:g}%", "valor": round(-base * rei / 100.0, 2)})
    if ica:
        out.append({"concepto": f"ReteICA {ab} {ica:g}%", "valor": round(-base * ica / 100.0, 2)})
    return out
