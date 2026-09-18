"""Atribución de la garantía por contrato.

Solo generan garantía los contratos **PLC** (Pague lo Contratado: si no cubren su
mínimo tienen que comprar en bolsa) o los que tienen un **duplicado** (energía
vendida que no existe → toda va a compra en bolsa). Los PLG sin duplicado son
ruido y quedan fuera.

El peso de cada contrato sale del **resumen de Cumplimiento**, que ya cruza el
compromiso contra la generación:
    peso = compras_bolsa_mwh (si es PLC) + exposicion_bolsa_duplicados_mwh (siempre)
El total de garantía (lo que estima el modelo, o el real del corte) se reparte
por ese peso. `repartir` y `pesos_por_contrato_desde_resumen` son PURAS; lo que
toca la base (`plc_numeros`, el resumen, la persistencia) está aislado y se puede
inyectar en las pruebas.

Nota (v2, mensual): el resumen netea el déficit al mes, así que un PLC de piso
HORARIO que cumple su total mensual (BIA, Nitro) sale en 0. Afinar eso pide los
mínimos horarios y es un paso aparte.
"""


def plc_numeros() -> set[str]:
    """`numero_codigo_contrato` de los contratos marcados PLC.

    El campo vive en `asic_solicitudes.modalidad_pago` ('plc'/'plg'); su código de
    contrato interno (`contrato_interno`) es el que el resumen usa como
    `numero_codigo_contrato`. Aislado (toca la base) para inyectar un doble en las
    pruebas.
    """
    from apps.mercado_xm.models import AsicSolicitud

    return set(
        AsicSolicitud.objects
        .filter(modalidad_pago="plc", contrato_interno__isnull=False)
        .values_list("contrato_interno", flat=True)
    )


def pesos_por_contrato_desde_resumen(contratos: list[dict],
                                     plc_numeros: set[str]) -> tuple[dict, dict]:
    """Peso de cada contrato para el reparto, desde el resumen de Cumplimiento.

    peso = compras_bolsa_mwh (si el contrato es PLC) + exposicion_bolsa_duplicados_mwh.
    Solo entran los de peso > 0 (los PLG sin duplicado dan 0 y quedan fuera).

    Devuelve `(pesos, meta)`: `pesos={numero_codigo: peso}` para `repartir`, y
    `meta={numero_codigo: {contrato, comprador, es_plc, es_duplicado}}`.
    """
    pesos: dict[str, float] = {}
    meta: dict[str, dict] = {}
    for c in contratos:
        num = c.get("numero_codigo_contrato")
        if not num:
            continue
        es_plc = num in plc_numeros
        dup = float(c.get("exposicion_bolsa_duplicados_mwh") or 0)
        compras = float(c.get("compras_bolsa_mwh") or 0)
        peso = (compras if es_plc else 0.0) + dup
        if peso <= 0:
            continue
        pesos[num] = round(peso, 6)
        meta[num] = {
            "contrato": c.get("nombre_interno"),
            "comprador": c.get("comprador_nombre"),
            "es_plc": es_plc,
            "es_duplicado": dup > 0,
        }
    return pesos, meta


def repartir(por_contrato: dict[str, float], total_garantia: float) -> list[dict]:
    """Reparte `total_garantia` entre los contratos proporcional a su peso.

    `por_contrato` = {codigo: peso_mwh}. Una fila por contrato con peso > 0,
    ordenada de mayor a menor monto: {'codigo', 'deficit_mwh', 'pct', 'monto'}.
    Suma exacta a `total_garantia`. Vacío si no hay peso.
    """
    positivos = {c: m for c, m in por_contrato.items() if m and m > 0}
    total_mwh = sum(positivos.values())
    if total_mwh <= 0:
        return []
    filas = []
    for codigo, mwh in positivos.items():
        pct = mwh / total_mwh
        filas.append({
            "codigo": codigo,
            "deficit_mwh": round(mwh, 6),
            "pct": round(pct, 6),
            "monto": round(total_garantia * pct, 2),
        })
    filas.sort(key=lambda f: f["monto"], reverse=True)
    return filas


def garantia_por_contrato(year: int, month: int, total_garantia: float, *,
                          resumen_fn=None, plc_fn=plc_numeros) -> list[dict]:
    """Reparto de la garantía del mes entre los contratos que la generan.

    Pipeline: resumen de Cumplimiento → pesos (PLC + duplicados) → reparte el
    total. `resumen_fn` y `plc_fn` se inyectan en las pruebas.
    """
    if resumen_fn is None:
        from apps.mercado_xm.services.cumplimiento.resumen import resumen as resumen_fn

    contratos = resumen_fn(year, month, incluir_todos=True).get("contratos") or []
    pesos, meta = pesos_por_contrato_desde_resumen(contratos, plc_fn())
    filas = repartir(pesos, total_garantia)
    for f in filas:
        f.update(meta.get(f["codigo"], {}))
    return filas


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------

def guardar_atribucion(fecha_corte, anio: int, mes: int, total_garantia: float,
                       filas: list[dict]) -> int:
    """Persiste el reparto de un corte (reemplaza el del mismo corte+período)."""
    from apps.garantias import models

    models.GarantiaContrato.objects.filter(
        fecha_corte=fecha_corte, anio=anio, mes=mes
    ).delete()
    objs = [
        models.GarantiaContrato(
            fecha_corte=fecha_corte, anio=anio, mes=mes,
            codigo=f["codigo"], contrato=f.get("contrato"),
            comprador=f.get("comprador"), proyecto_id=f.get("proyecto_id"),
            deficit_mwh=f["deficit_mwh"], pct=f["pct"], monto=f["monto"],
            total_garantia=total_garantia,
        )
        for f in filas
    ]
    models.GarantiaContrato.objects.bulk_create(objs)
    return len(objs)


def leer_atribucion(anio: int, mes: int, fecha_corte=None) -> list[dict]:
    """Reparto guardado de un período. Sin `fecha_corte`, el del corte más reciente."""
    from apps.garantias import models

    qs = models.GarantiaContrato.objects.filter(anio=anio, mes=mes)
    if fecha_corte is None:
        ultima = qs.order_by("-fecha_corte").values_list("fecha_corte", flat=True).first()
        if ultima is None:
            return []
        fecha_corte = ultima
    filas = qs.filter(fecha_corte=fecha_corte).order_by("-monto")
    return [
        {
            "codigo": f.codigo, "contrato": f.contrato, "comprador": f.comprador,
            "proyecto_id": f.proyecto_id,
            "deficit_mwh": float(f.deficit_mwh), "pct": float(f.pct),
            "monto": float(f.monto), "total_garantia": float(f.total_garantia),
            "fecha_corte": f.fecha_corte.isoformat(),
        }
        for f in filas
    ]
