"""Los agregados que el proxy calcula sobre lo que devuelve la API externa.

Todo esto se hace ACÁ y no en el frontend porque son reglas —qué IPP está
vigente, qué costo se cruza con el mes, cuántos ceros se ocultaron— y una regla
repetida en el cliente se desincroniza.
"""

HORAS_DEL_DIA = 24


def build_facturas_xm(datos: dict) -> dict:
    """Traduce el bloque de alistamiento (`readiness`) al español."""
    alistamiento = datos.get("readiness") or {}
    return {
        "count": datos.get("count") or 0,
        "readiness": {
            "lista_para_repartir": bool(
                alistamiento.get("ready_for_distribution")
            ),
            "total": alistamiento.get("total") or 0,
            "completadas": alistamiento.get("completed") or 0,
            "tiene_factura_generador": bool(
                alistamiento.get("has_generator_invoice")
            ),
            "tiene_factura_comercializador": bool(
                alistamiento.get("has_commercializer_invoice")
            ),
            "bloqueos": alistamiento.get("blockers") or [],
            "sin_completar": alistamiento.get("not_completed") or [],
            "totales_invalidos": alistamiento.get("invalid_totals") or [],
        },
        "results": [
            {
                "id": f.get("id"), "codigo": f.get("codigo"),
                "nombre": f.get("nombre"), "agente": f.get("agente"),
                "mes": f.get("month"), "mes_nombre": f.get("month_display"),
                "anio": f.get("year"), "version": f.get("version"),
                "periodo_inicio": f.get("periodo_inicio"),
                "periodo_fin": f.get("periodo_fin"),
                "vencimiento": f.get("vencimiento"),
                "procesada_el": f.get("processed_at"),
                "estado_procesamiento": f.get("processing_status"),
                "error": f.get("error_message"),
                "valor_total": f.get("valor_total"),
                "total_declarado": f.get("total_amount"),
                "total_valido": f.get("is_total_valid"),
                "campos_extraidos": f.get("fields_count"),
            }
            for f in (datos.get("results") or [])
        ],
    }


def _mas_reciente_primero(filas) -> None:
    filas.sort(
        key=lambda f: (str(f.get("date") or ""), str(f.get("project") or "")),
        reverse=True,
    )


def build_despachos(filas: list[dict], nombres: dict) -> dict:
    _mas_reciente_primero(filas)
    return {
        "count": len(filas),
        "results": [
            {
                "id": f.get("id"),
                "topico": f.get("project"),
                # Nombre de esta base; si el tópico no cruza, se deja el tópico.
                "proyecto": (
                    nombres.get(f.get("project") or "") or f.get("project")
                ),
                "fecha": f.get("date"),
                "tipo_dato": f.get("data_type"),
                "energia_kwh": f.get("energy"),
                "valor": f.get("price"),
                "codigo_contrato": f.get("contract_code"),
                "contrato_proyecto_id": f.get("contract_energy_project"),
                "version": f.get("version"),
            }
            for f in filas
        ],
    }


def build_consumo(filas: list[dict], nombres: dict) -> dict:
    """Energía contratada hora por hora (`con_hour01`..`con_hour24`, en kWh).

    El total diario se calcula acá y no se pide a la API: ese campo no existe
    allá.
    """
    _mas_reciente_primero(filas)
    resultados = []
    for fila in filas:
        horas = [
            fila.get(f"con_hour{h:02d}") for h in range(1, HORAS_DEL_DIA + 1)
        ]
        resultados.append({
            "id": fila.get("id"),
            "topico": fila.get("project"),
            "proyecto": (
                nombres.get(fila.get("project") or "") or fila.get("project")
            ),
            "fecha": fila.get("date"),
            "version": fila.get("version"),
            "horas": horas,
            "total_diario": round(
                sum(h for h in horas if h is not None), 4
            ),
        })
    return {"count": len(resultados), "results": resultados}


def build_ipp_historico(filas: list[dict]) -> list[dict]:
    """Marca qué consulta del IPP es la VIGENTE de cada período.

    Puede haber varias filas por mes —cada consulta al DANE deja la suya— y la
    que vale es la de fecha más reciente.
    """
    vigentes: dict[tuple, str] = {}
    for fila in filas:
        clave = (fila.get("year"), fila.get("month"))
        fecha = str(fila.get("date") or "")
        if fecha >= vigentes.get(clave, ""):
            vigentes[clave] = fecha

    ordenadas = sorted(
        filas,
        key=lambda f: (
            f.get("year") or 0, f.get("month") or 0, str(f.get("date") or "")
        ),
        reverse=True,
    )
    return [
        {
            "id": f.get("id"),
            "anio": f.get("year"),
            "mes": f.get("month"),
            "ipp": f.get("ipp"),
            "consultado_el": f.get("date"),
            "vigente": (
                str(f.get("date") or "")
                == vigentes.get((f.get("year"), f.get("month")))
            ),
        }
        for f in ordenadas
    ]


def _valor_de_costo(costo: dict) -> float | None:
    """El valor como número, o `None` si la API mandó algo que no lo es.

    No levanta: UNA fila con el valor corrupto tumbaba la pantalla entera con un
    500, y el resto de los costos son perfectamente legibles.
    """
    try:
        return float(costo["value"])
    except (KeyError, TypeError, ValueError):
        return None


def build_costos(
    filas: list[dict], tipos: dict, nombres: dict,
    grupo=None, mes=None, anio=None, solo_con_valor=True,
    page: int = 1, size: int = 100,
) -> dict:
    """Costos e ingresos fijos, filtrados y paginados AQUÍ.

    La API externa devuelve la tabla completa (más de 10 000 filas) sin paginar,
    así que el corte se hace de este lado para no mandarle eso al navegador. Por
    lo mismo el filtro por grupo se aplica ANTES de paginar: si no, `total`
    contaría filas que la página no muestra.

    Más de la mitad de esas filas valen cero, y no por casualidad: el reparto le
    crea una fila de cada concepto a TODOS los proyectos, así que un proyecto
    que no es comercializador arrastra su `iva_comercializador` en cero. Por eso
    se ocultan por defecto — pero `ocultos_en_cero` viaja siempre, para poder
    decir cuántos hay en vez de dar a entender que no existen.
    """
    if grupo is not None:
        filas = [
            c for c in filas
            if (tipos.get(c.get("payment_type") or "") or {}).get("group") == grupo
        ]

    if anio is not None:
        # Se queda el costo cuya vigencia se CRUZA con el período pedido: un
        # costo anual cubre doce meses, así que basta el traslape.
        desde = f"{anio}-{mes:02d}-01" if mes else f"{anio}-01-01"
        hasta = f"{anio}-{mes:02d}-31" if mes else f"{anio}-12-31"
        filas = [
            c for c in filas
            if (c.get("from_date") or "0000-01-01") <= hasta
            and (c.get("to_date") or "9999-12-31") >= desde
        ]

    # Los ceros se cuentan sobre el resto de filtros YA aplicados: «2 208 en
    # cero ocultas» tiene que referirse a lo que se está mirando.
    # Un valor ilegible NO es un cero: se muestra, para que se note.
    ocultos = sum(1 for c in filas if _valor_de_costo(c) == 0)
    if solo_con_valor:
        filas = [c for c in filas if _valor_de_costo(c) != 0]

    # Lo más reciente primero: es lo que se está liquidando.
    filas.sort(key=lambda c: (c.get("from_date") or ""), reverse=True)
    inicio = (page - 1) * size

    return {
        "ocultos_en_cero": ocultos,
        "total": len(filas),
        "page": page,
        "size": size,
        "results": [
            {
                "id": c.get("id"),
                "proyecto": (
                    nombres.get(c.get("project") or "") or c.get("project")
                ),
                "tipo_pago": c.get("payment_type"),
                "tipo_pago_nombre": (
                    tipos.get(c.get("payment_type") or "") or {}
                ).get("long_name"),
                "grupo": (
                    tipos.get(c.get("payment_type") or "") or {}
                ).get("group"),
                "valor": _valor_de_costo(c),
                "fecha_desde": c.get("from_date"),
                "fecha_hasta": c.get("to_date"),
                "frecuencia_pago": c.get("payment_frecuency"),
                "version": c.get("version"),
            }
            for c in filas[inicio:inicio + size]
        ],
    }


def build_contratos_energia(
    contratos, vinculos, cantidades, catalogos, nombres,
) -> list[dict]:
    """Contratos con sus proyectos, marcando si tienen piso y techo.

    Un contrato PLC sin los dos hace fallar la liquidación, y hoy no hay otro
    sitio donde verlo.
    """
    empresas = {e["id"]: e.get("nombre_empresa") for e in catalogos["empresas"]}
    precios = {p["id"]: p.get("name") for p in catalogos["precios_energia"]}

    # Las curvas enteras, no solo si existen: para poder EDITARLAS hace falta el
    # id de cada una y sus 24 horas. `tiene_piso`/`tiene_techo` se conservan
    # porque es lo que pinta la tabla.
    curvas: dict[int, dict[str, dict]] = {}
    for cantidad in cantidades:
        curvas.setdefault(cantidad.get("contract_energy_project"), {})[
            cantidad.get("concept_type")
        ] = {"id": cantidad.get("id"), "hours": cantidad.get("hours")}

    por_contrato: dict[int, list[dict]] = {}
    for vinculo in vinculos:
        propias = curvas.get(vinculo.get("id"), {})
        topico = vinculo.get("project")
        por_contrato.setdefault(vinculo.get("contract_energy"), []).append({
            "id": vinculo.get("id"),
            # El tópico ademas del nombre: es la clave que usa la API y con la
            # que el formulario de edición precarga el desplegable.
            "topico": topico,
            "proyecto": nombres.get(topico or "") or topico,
            "precio_energia_id": vinculo.get("energy_price"),
            "precio_energia": precios.get(vinculo.get("energy_price")),
            "piso": propias.get("floor"),
            "techo": propias.get("roof"),
            "tiene_piso": "floor" in propias,
            "tiene_techo": "roof" in propias,
        })

    return [
        {
            "id": c["id"],
            "fecha_desde": c.get("date_from"),
            "fecha_hasta": c.get("date_to"),
            "codigo": c.get("code"),
            "tipo_contrato": c.get("contract_type"),
            "tipo_tarifa": c.get("tariff_price_type"),
            "porcentaje": c.get("percentage"),
            "empresa_id": c.get("company"),
            "empresa": empresas.get(c.get("company")),
            "proyectos": por_contrato.get(c["id"], []),
        }
        for c in contratos
    ]
