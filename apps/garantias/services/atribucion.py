"""Atribución de la garantía por contrato.

La garantía la genera el DÉFICIT horario (la compra en bolsa que fuerzan los
mínimos horarios de ciertos contratos). El BalCttos —cruzado hora a hora por
`app.services.balcttos.deficit_por_contrato`— dice cuánto déficit aporta cada
contrato; acá se reparte el total de garantía por ese peso, para poder cobrarle
a cada cliente su parte.

`repartir` es PURA (peso → monto). El mapeo de código de contrato a proyecto y
cliente vive en `mapear_contratos`, que sí toca la base.
"""


def mapear_contratos(codigos: list[str]) -> dict[str, dict]:
    """{codigo_sic_contrato: {'proyecto_id', 'proyecto', 'contrato_interno'}}.

    Aislado (toca la base) para poder inyectar un doble en las pruebas, igual que
    `_plantas_contratos_de` en el balance. Un código puede tener varias versiones
    en `asic_solicitudes` (modificaciones): gana la más reciente con proyecto.
    """
    from apps.mercado_xm.models import AsicSolicitud

    filas = (
        AsicSolicitud.objects
        .filter(codigo_sic_contrato__in=[c for c in codigos if c])
        .select_related("proyecto")
        .order_by("codigo_sic_contrato", "-fecha_inicio", "-id")
    )
    salida: dict[str, dict] = {}
    for a in filas:
        cod = a.codigo_sic_contrato
        if cod in salida and salida[cod]["proyecto_id"] is not None:
            continue  # ya tenemos una versión con proyecto
        salida[cod] = {
            "proyecto_id": a.proyecto_id,
            "proyecto": a.proyecto.nombre_comercial if a.proyecto_id else None,
            "contrato_interno": a.nombre_interno or a.contrato_interno,
        }
    return salida


def garantia_por_contrato_de_bytes(contenido: bytes, total_garantia: float, *,
                                   mapear_fn=mapear_contratos) -> list[dict]:
    """Pipeline completo: parsea el BalCttos, mapea a proyecto/cliente y reparte
    `total_garantia` por el déficit de cada contrato. `mapear_fn` inyectable."""
    from app.services.balcttos import deficit_por_contrato_de_bytes

    parseado = deficit_por_contrato_de_bytes(contenido)
    por_contrato = parseado["por_contrato"]
    mapa = mapear_fn(list(por_contrato.keys()))
    return atribuir(por_contrato, total_garantia, mapa=mapa,
                    comprador=parseado["comprador"])


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


def repartir(por_contrato: dict[str, float], total_garantia: float) -> list[dict]:
    """Reparte `total_garantia` entre los contratos proporcional a su déficit.

    `por_contrato` = {codigo: deficit_mwh}. Devuelve una fila por contrato con
    déficit > 0, ordenada de mayor a menor monto:
        {'codigo', 'deficit_mwh', 'pct', 'monto'}.
    Suma exacta a `total_garantia`. Vacío si no hay déficit.
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


def atribuir(por_contrato: dict[str, float], total_garantia: float, *,
             mapa: dict[str, dict], comprador: dict[str, str]) -> list[dict]:
    """`repartir` + enriquecido con contrato/proyecto/cliente.

    `mapa` = {codigo: {'proyecto_id', 'proyecto', 'contrato_interno'}} (de
    `mapear_contratos`); `comprador` = {codigo: codigo_sic_comprador} (del
    BalCttos). Un código sin mapa NO se pierde: queda con contrato/proyecto en
    None, para que la suma siga cuadrando con el total.
    """
    filas = repartir(por_contrato, total_garantia)
    for f in filas:
        m = mapa.get(f["codigo"]) or {}
        f["contrato"] = m.get("contrato_interno")
        f["proyecto"] = m.get("proyecto")
        f["proyecto_id"] = m.get("proyecto_id")
        f["comprador"] = comprador.get(f["codigo"])
    return filas
