"""Los costos del Panel Contable que NO salen del Estado de Resultados.

Puerto de `app/services/costos_panel.py`.

**Cada costo tiene UNA fuente de verdad y este módulo la consulta, no la
reimplementa**: Mantenimiento sale de O&M (`om.panel.valor_de_proyecto`),
Arrendamiento de Arriendos, Internet de su contrato de servicio y Starlink de su
factura.

**La factura de Starlink manda sobre el contrato de internet**: solo 4 proyectos
tienen tarifa de internet cargada y Starlink factura 40. Por eso va después — para
pisar lo del contrato cuando existan los dos.

El IVA no es uniforme: Mantenimiento e Internet llevan 19 % plano (como el ER) y
se marcan con el flag `iva`; Arriendos trae el suyo POR ARRENDADOR, así que viaja
como `iva_valor` y solo hay línea si algún arrendador es responsable de IVA.
"""

from __future__ import annotations


# Conceptos del ER que el módulo controla. Coinciden EXACTO con las etiquetas que
# emite el parser (_COSTO_CONCEPTOS en er_loader).
CONCEPTO_OM = "Mantenimiento"
CONCEPTO_ARRIENDO = "Arrendamiento"
CONCEPTO_INTERNET = "Servicio de Internet"


def _valor_internet(proyecto, periodo: str) -> float | None:
    """Tarifa mensual de internet (indexada) del proyecto. None si no tiene contrato
    de internet o si el contrato no tiene tarifa/indexación para calcular (se conserva
    el ER); 0 si el contrato no está vigente."""
    from apps.contratos.models import ContratoServicio

    c = (
        ContratoServicio.objects
        .filter(servicio_aplica="internet", proyecto_id=proyecto.id)
        .order_by("id")
        .first()
    )
    if c is None:
        return None
    if c.estado != "firmado":
        return 0.0
    idx = c.indexacion_anual or c.indexacion_mensual or []
    t = _tarifa_indexada_periodo(idx, c.tarifa_mensual, c.fecha_firma_contrato, periodo)
    return float(t) if t is not None else None


def valores_modulo_costos(proyecto_id: int, periodo: str) -> dict[str, dict]:
    """{concepto: {"valor": <neg>, "fuente": str, ...}} para los conceptos que el
    módulo controla. Solo incluye un concepto si el proyecto tiene contrato de ese
    servicio (si no, el Panel conserva el valor del ER).

    Mantenimiento y Arrendamiento se PIDEN a sus módulos (única fuente de verdad), no
    se recalculan aquí. Internet se lee de su contrato de servicio."""
    from apps.arriendos.services.panel import valor_de_proyecto as valor_arriendo
    from apps.om.services.panel import valor_de_proyecto as valor_om
    from apps.proyectos.models import Proyecto

    proyecto = Proyecto.objects.filter(pk=proyecto_id).first()
    if proyecto is None:
        return {}

    out: dict[str, dict] = {}

    v = valor_om(proyecto_id, periodo)
    if v is not None:
        # Mantenimiento siempre lleva IVA 19% (como el ER): flag `iva` → lo recalcula el merge.
        # alias: algunos ER lo etiquetan "Fondo de mantenimiento" (es el mismo costo).
        out[CONCEPTO_OM] = {"valor": -abs(v), "fuente": "om", "iva": True,
                            "alias": ["Fondo de mantenimiento"]}

    r = valor_arriendo(proyecto_id, periodo)
    if r is not None:
        canon, iva = r
        # El IVA de arriendo lo trae el módulo por arrendador (no un 19% plano):
        # solo hay línea de IVA si algún arrendador es responsable de IVA.
        out[CONCEPTO_ARRIENDO] = {
            "valor": -abs(canon), "fuente": "arriendos",
            "iva_valor": (-abs(iva) if iva else None),
        }

    # Internet: tarifa mensual fija (indexada) del contrato. IVA 19% como el ER.
    v = _valor_internet(proyecto, periodo)
    if v is not None:
        out[CONCEPTO_INTERNET] = {"valor": -abs(v), "fuente": "internet", "iva": True}

    # La factura de Starlink manda sobre el contrato: solo 4 proyectos tienen
    # tarifa de internet cargada, y Starlink factura 40. Va después a propósito,
    # para pisar lo del contrato cuando existan ambos.
    from apps.monitoreo.models import StarlinkFacturaLinea

    lineas_sl = list(
        StarlinkFacturaLinea.objects.filter(
            proyecto_id=proyecto_id, factura__periodo=periodo,
        )
    )
    sl = internet_desde_starlink(lineas_sl)
    if sl:
        # El IVA del ER es 19% plano; el de Starlink viene en la factura, así que
        # se conserva el flag para que el merge lo recalcule igual que antes.
        sl[CONCEPTO_INTERNET]["iva"] = True
        out.update(sl)

    return out


def _tarifa_indexada_periodo(indexacion, tarifa_base, fecha_firma, periodo: str) -> float | None:
    """Tarifa ($/kWh) vigente en `periodo` según la indexación por aniversario.

    `indexacion` es la lista JSONB del contrato: [{"año","valor","esBase"?}, ...],
    un valor por aniversario de firma (IPC anual encadenado, ya pre-calculado). Se
    elige el aniversario más reciente que ya ocurrió al `periodo` (mes/día de firma).
    Si el período es anterior a todo aniversario o no hay lista, cae a la base."""
    try:
        py, pm = (int(x) for x in str(periodo).split("-")[:2])
    except Exception:
        return float(tarifa_base) if tarifa_base is not None else None
    mes_firma = fecha_firma.month if fecha_firma else 1
    mejor_val, mejor_key = None, None
    for e in (indexacion or []):
        val = e.get("valor")
        anio = e.get("año", e.get("anio", e.get("anno")))
        if val is None or anio is None:
            continue
        key = (int(anio), mes_firma)          # aniversario (año, mes de firma)
        if key <= (py, pm) and (mejor_key is None or key > mejor_key):
            mejor_key, mejor_val = key, float(val)
    if mejor_val is not None:
        return mejor_val
    # 'esBase' (repr/CGM) o 'es_base' (internet/O&M): distinto esquema de JSONB.
    base = next((e.get("valor") for e in (indexacion or [])
                 if e.get("esBase") or e.get("es_base")), None)
    if base is not None:
        return float(base)
    return float(tarifa_base) if tarifa_base is not None else None


def internet_desde_starlink(lineas) -> dict[str, dict]:
    """Internet del período a partir de la factura real de Starlink.

    ``lineas`` son las ``StarlinkFacturaLinea`` del proyecto en ese período. Se
    suman porque un proyecto puede tener más de un sitio (Perija tiene dos). El
    IVA no se devuelve: el Panel lo deriva por cliente al leer, y mandarlo aquí
    lo duplicaría.

    Manda sobre el contrato de servicio de internet, que solo tiene tarifa en 4
    proyectos mientras Starlink factura 40. Cruzado contra 2026-07: los 26
    proyectos con internet en el Panel cuadran al peso, 3.894.133 en ambos lados.

    Sin líneas devuelve ``{}`` y no ``0``: un cero pisaría el valor del ER con un
    dato falso.
    """
    vivas = [l for l in lineas if not getattr(l, "excluido", False)]
    if not vivas:
        return {}
    base = sum(float(l.sin_iva or 0) for l in vivas)
    return {CONCEPTO_INTERNET: {"grupo": "costos", "valor": -abs(round(base, 2)),
                                "fuente": "starlink"}}


def elegir_contrato_representacion(contratos):
    """El contrato de representación que manda cuando un proyecto tiene varios.

    Ahí viven las tarifas de Representación, CGM y Administración. Hay 66 filas
    para 38 proyectos y algunas se contradicen: Joropo tiene tres, dos con las
    tarifas en cero.

    Ordena por vigencia primero, luego por **cuántas** tarifas trae cargadas, y a
    igualdad de condiciones el más reciente. Antes se tomaba el de menor ``id``,
    que hoy acierta en los 38 proyectos por casualidad -- el de Joropo con tarifa
    tiene id menor que los dos de ceros. Basta borrar ese o crear uno con id menor
    para que empiece a leer ceros sin que nadie lo note.

    Cuenta las tarifas en vez de preguntar si tiene alguna: los tres contratos de
    Joropo traen ``tarifa_admin``, así que con un booleano los tres empatarían y
    ganaría el de id más alto, que es uno de los que tiene Representación y CGM
    en cero. Contándolas gana el 108, que trae las tres.

    La vigencia pesa más que las tarifas: un contrato vigente en ceros es una
    decisión de negocio, no un dato faltante.
    """
    if not contratos:
        return None

    def _puntaje(c):
        tarifas = sum(1 for t in (c.tarifa_representacion, c.tarifa_cgm, c.tarifa_admin) if t)
        return (c.estado == "firmado", tarifas, c.id)

    return max(contratos, key=_puntaje)


def valores_facturas_modulo(proyecto_id: int, periodo: str,
                            kwh: float | None, ingreso: float | None = None) -> dict[str, dict]:
    """Servicios del grupo 'facturas' desde las tarifas de la app (no del ER):
    - Representación / CGM = tarifa indexada × energía (kWh) del mes.
    - Administración = tarifa_admin (%) × ingreso del mes.

    kWh e ingreso los pasa el caller y salen del ER (decisión de negocio: deben
    cuadrar con el ER). Repr/CGM/Admin viven en un mismo contrato
    `servicio_aplica='representacion'` (ahí está también `tarifa_admin`; la admin es
    el fee de operación). Solo se devuelve un concepto si el contrato tiene su tarifa.
    Valores negativos. Los impuestos NO se calculan aquí: el Panel los deriva por
    cliente al leer."""
    from apps.contratos.models import ContratoServicio
    from apps.proyectos.models import Proyecto

    if not Proyecto.objects.filter(pk=proyecto_id).exists():
        return {}
    c = elegir_contrato_representacion(
        list(ContratoServicio.objects.filter(
            servicio_aplica="representacion", proyecto_id=proyecto_id,
        ))
    )
    if c is None:
        return {}

    out: dict[str, dict] = {}
    if kwh:
        t_rep = _tarifa_indexada_periodo(c.indexacion_representacion, c.tarifa_representacion,
                                         c.fecha_firma_contrato, periodo)
        if t_rep:
            out["Representación"] = {"grupo": "facturas", "valor": -abs(round(t_rep * kwh, 2)),
                                     "fuente": "servicios"}
        t_cgm = _tarifa_indexada_periodo(c.indexacion_cgm, c.tarifa_cgm,
                                         c.fecha_firma_contrato, periodo)
        if t_cgm:
            out["CGM"] = {"grupo": "facturas", "valor": -abs(round(t_cgm * kwh, 2)),
                          "fuente": "servicios"}
    # Administración = fee de operación = tarifa_admin (%) × ingreso del mes.
    if c.tarifa_admin and ingreso:
        out["Administración"] = {"grupo": "facturas",
                                 "valor": -abs(round(float(c.tarifa_admin) * float(ingreso), 2)),
                                 "fuente": "operacion"}
    return out


def aplicar_costos_modulo(base: list[dict], mods: dict[str, dict], iva: float = 0.19) -> list[dict]:
    """Mezcla las líneas base del ER con los valores del módulo (lógica pura).

    Para cada concepto en `mods`:
    - Si existe una línea de costos con ese concepto, reemplaza su valor y marca
      `fuente` (y borra hoja/celda: ya no viene del ER).
    - Si no existe, la agrega.
    - IVA de la línea "IVA <concepto>":
        · `iva_valor` (monto con signo) ⇒ se usa tal cual; None ⇒ sin línea de IVA
          (y si existía una del ER, se elimina). Caso Arriendos: el IVA lo trae el
          módulo por arrendador.
        · en su defecto, `iva`=True ⇒ 19% plano sobre el valor. Caso Mantenimiento.
    - `alias`: otros nombres con los que el ER pudo traer el MISMO concepto (ej.
      "Fondo de mantenimiento" = "Mantenimiento"). Se renombran al canónico antes de
      mezclar, para reemplazar esa línea en vez de duplicarla.

    No muta la lista de entrada; devuelve una nueva.
    """
    out = [dict(l) for l in base]

    # Normalizar alias: renombrar al concepto canónico lo que el ER trajo con otro
    # nombre (concepto y su línea de IVA), así el override lo reemplaza sin duplicar.
    for concepto, info in mods.items():
        grupo = info.get("grupo", "costos")
        for alias in info.get("alias", []):
            for l in out:
                if l.get("grupo") != grupo:
                    continue
                if l.get("concepto") == alias:
                    l["concepto"] = concepto
                elif l.get("concepto") == f"IVA {alias}":
                    l["concepto"] = f"IVA {concepto}"

    def _find(grupo: str, concepto: str) -> int | None:
        for i, l in enumerate(out):
            if l.get("grupo") == grupo and l.get("concepto") == concepto:
                return i
        return None

    for concepto, info in mods.items():
        valor = info["valor"]
        fuente = info.get("fuente")
        grupo = info.get("grupo", "costos")     # 'costos' (O&M/arriendo) | 'facturas' (repr/CGM)
        idx = _find(grupo, concepto)
        linea = {"grupo": grupo, "concepto": concepto, "valor": valor,
                 "hoja": None, "celda": None, "fuente": fuente}
        if idx is None:
            out.append(linea)
            idx = len(out) - 1
        else:
            out[idx].update(linea)

        # Monto de IVA guardado como línea derivada: explícito (`iva_valor`, caso
        # Arriendos) o 19% plano por flag `iva` (Mantenimiento). Los servicios del
        # grupo 'facturas' (Repr/CGM) NO guardan IVA aquí: el Panel deriva sus
        # impuestos por cliente en tiempo de lectura, así que nunca traen iva/iva_valor.
        if "iva_valor" in info:
            iva_monto = info["iva_valor"]
        elif info.get("iva"):
            iva_monto = round(valor * iva, 2)
        else:
            iva_monto = None

        iva_concepto = f"IVA {concepto}"
        j = _find(grupo, iva_concepto)
        if iva_monto is None:
            if j is not None:      # el ER traía IVA pero el módulo dice que no lleva
                out.pop(j)
            continue
        iva_linea = {"grupo": grupo, "concepto": iva_concepto, "valor": iva_monto,
                     "hoja": None, "celda": None, "fuente": fuente}
        if j is None:
            out.insert(idx + 1, iva_linea)   # justo después del concepto, como el ER
        else:
            out[j].update(iva_linea)

    return out
