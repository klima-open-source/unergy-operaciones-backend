"""El cálculo de facturación de energía del mes.

    facturación = kWh(despacho) × tarifa_indexada
    tarifa_indexada = round(tarifa_base_PPA × IPP_mes / IPP_base_PPA, 2)

**El redondeo va ANTES de multiplicar**, igual que en el Excel de la usuaria.
Redondear al final da centavos distintos y las cifras dejan de cuadrar con el
suyo, que es el que se contrasta.

Fuentes: `despacho_contrato_mensual` (del archivo XM), `AsicSolicitud` para
saber a qué PPA pertenece cada contrato SIC vigente, `PpaTarifa` + `IppMensual`
para la tarifa, y `PrecioBolsaMensual` para valorizar lo que no tiene PPA.
"""

from calendar import monthrange
from datetime import date

from apps.facturacion import models as fa_models
from apps.facturacion.services.factura_mensaje import (
    construir_mensaje, contribuciones,
)
from apps.mercado_xm import models as mx_models
from apps.mercado_xm.services.gescon_vigencia import resolver_vigencias
from apps.ppa import models as ppa_models
from apps.proyectos import models as py_models


def periodo_valido(periodo: str) -> str:
    """Normaliza `YYYY-M` a `YYYY-MM`. Levanta `ValueError` si no cuadra."""
    try:
        anio, mes = periodo.strip().split("-")
        return f"{int(anio):04d}-{int(mes):02d}"
    except Exception as exc:
        raise ValueError("El período debe tener formato YYYY-MM") from exc


def _dueños_por_contrato(ultimo_dia: date) -> dict:
    """Qué solicitud ASIC manda sobre cada código SIC al cierre del período.

    Además de las VIGENTES se incluyen las salientes por relevo: un contrato
    terminado a mitad de mes queda `vigente=False` pero su ventana recortada
    sigue contando, y el despacho ya trae solo los días que operó. Sin este
    respaldo el código perdía su dueño y su energía caía a «sin PPA», valorada a
    bolsa en vez de facturarse a su PPA (caso SIC 89902 / GD San Pelayo →
    Terpel 8, julio de 2026).
    """
    universo = list(
        mx_models.AsicSolicitud.objects
        .filter(estado_solicitud="publicado")
        .exclude(tipo_solicitud="desistimiento")
        .order_by("fecha_inicio", "fecha_solicitud", "created_at")
    )
    vigencias = resolver_vigencias(universo, hasta=ultimo_dia)

    dueños: dict = {}
    salientes: dict = {}
    for solicitud in universo:
        vigencia = vigencias.get(solicitud.id)
        if vigencia is None or not vigencia.procesado:
            continue
        codigo = (solicitud.codigo_sic_contrato or "").strip()
        if not codigo:
            continue
        if vigencia.vigente:
            dueños[codigo] = solicitud
        elif vigencia.saliente_por_relevo:
            # Con varios salientes del mismo código gana el que cerró más tarde:
            # es el último dueño del período.
            previo = salientes.get(codigo)
            fin = vigencia.fecha_fin_efectiva or date.min
            if previo is None or (
                vigencias[previo.id].fecha_fin_efectiva or date.min
            ) < fin:
                salientes[codigo] = solicitud
    for codigo, solicitud in salientes.items():
        dueños.setdefault(codigo, solicitud)
    return dueños


def _lineas(periodo: str, anio: int, mes: int, ipp, dueños, agrupaciones):
    ppas = {p.id: p for p in ppa_models.PpaContrato.objects.all()}
    tarifas = {
        t.contrato_id: (float(t.tarifa) if t.tarifa is not None else None)
        for t in ppa_models.PpaTarifa.objects.filter(**{"año": anio, "mes": mes})
    }
    nombres = dict(
        py_models.Proyecto.objects.values_list("id", "nombre_comercial")
    )

    lineas = []
    despacho = mx_models.DespachoContratoMensual.objects.filter(periodo=periodo)
    for fila in despacho:
        codigo = fila.codigo_sic_contrato
        solicitud = dueños.get(codigo)
        ppa_id = solicitud.contrato_ppa_id if solicitud else None
        proyecto_id = solicitud.proyecto_id if solicitud else None
        ppa = ppas.get(ppa_id) if ppa_id else None
        base = tarifas.get(ppa_id) if ppa_id else None
        ipp_base = (
            float(ppa.valor_indexacion_base)
            if (ppa and ppa.valor_indexacion_base) else None
        )

        estado, tarifa_idx, facturacion = _estado_de_linea(
            ppa_id, base, ipp_base, ipp, float(fila.kwh)
        )
        nombre_ppa = (
            (ppa.nombre_interno or ppa.numero_codigo_contrato) if ppa else None
        )
        agrupacion = agrupaciones.get(codigo)

        lineas.append({
            "contrato": codigo,
            "comprador": fila.comprador,
            "ppa_id": ppa_id,
            "proyecto_id": proyecto_id,
            "proyecto": nombres.get(proyecto_id) if proyecto_id else None,
            "ppa": nombre_ppa,
            # Nombre de la factura: la agrupación manual del CONTRATO si existe,
            # si no el del PPA.
            "factura": (
                agrupacion[0] if agrupacion and agrupacion[0] else nombre_ppa
            ),
            "numero_contrato": ppa.numero_codigo_contrato if ppa else None,
            "periodo_ipp_base": ppa.periodo_indexacion_base if ppa else None,
            "dias": fila.dias,
            "fecha_min": fila.fecha_min,
            "fecha_max": fila.fecha_max,
            "kwh": float(fila.kwh),
            "tarifa_base": base,
            "ipp_base": ipp_base,
            "ipp_mes": ipp,
            "tarifa_indexada": tarifa_idx,
            "facturacion": facturacion,
            "estado": estado,
        })
    return lineas


def _estado_de_linea(ppa_id, base, ipp_base, ipp, kwh):
    """Por qué una línea no se puede facturar, o su valor si sí se puede.

    El orden de los `elif` es el orden en que la usuaria arregla los datos: sin
    PPA no hay nada más que mirar, y sin tarifa no importa el IPP.
    """
    if not ppa_id:
        return "sin_ppa", None, None
    if base is None:
        return "sin_tarifa", None, None
    if not ipp_base:
        return "sin_ipp_base", None, None
    if ipp is None:
        return "sin_ipp_mes", None, None
    tarifa_idx = round(base * ipp / ipp_base, 2)
    return "ok", tarifa_idx, round(kwh * tarifa_idx, 2)


def periodo(per: str) -> dict:
    """El cálculo completo del mes: líneas, agrupación por SIC y por factura."""
    anio, mes = int(per[:4]), int(per[5:7])
    ultimo_dia = date(anio, mes, monthrange(anio, mes)[1])

    ipp_fila = ppa_models.IppMensual.objects.filter(
        **{"año": anio, "mes": mes}
    ).first()
    ipp = float(ipp_fila.valor) if ipp_fila else None

    # Precio de bolsa para valorizar la energía SIN PPA (UNGC). Lo carga la
    # usuaria cada mes: es su promedio horario→diario, que NO coincide con el
    # promedio simple de `precios_bolsa_diario`. Sin respaldo automático a
    # propósito — si no está, esa energía queda sin valorizar hasta que lo ponga.
    bolsa_fila = mx_models.PrecioBolsaMensual.objects.filter(
        **{"año": anio, "mes": mes}
    ).first()
    bolsa = float(bolsa_fila.valor) if bolsa_fila else None

    agrupaciones = {
        a.codigo_sic_contrato: (
            a.nombre, float(a.porcentaje) if a.porcentaje is not None else None
        )
        for a in fa_models.FacturaAgrupacion.objects.all()
    }

    lineas = _lineas(
        per, anio, mes, ipp, _dueños_por_contrato(ultimo_dia), agrupaciones
    )
    facturables = [l for l in lineas if l["estado"] == "ok"]

    por_factura = _agrupar_por_factura(facturables, agrupaciones)
    _agregar_sin_ppa(por_factura, lineas, bolsa)
    _completar_facturas(por_factura, per, ipp)

    total_ppa = round(sum(l["facturacion"] or 0 for l in facturables), 2)
    ingreso_bolsa = round(
        sum(g["facturacion"] for g in por_factura.values() if g.get("sin_ppa")), 2
    )

    return {
        "periodo": per,
        "ipp_mes": ipp,
        "bolsa_precio": bolsa,
        "bolsa_manual": bolsa,
        "resumen": {
            "contratos": len(lineas),
            "facturables": len(facturables),
            "sin_ppa": sum(1 for l in lineas if l["estado"] == "sin_ppa"),
            "kwh_total": round(sum(l["kwh"] for l in facturables), 2),
            "kwh_bolsa": round(
                sum(l["kwh"] for l in lineas if l["estado"] == "sin_ppa"), 2
            ),
            "facturacion_total": total_ppa,
            "ingreso_bolsa": ingreso_bolsa,
            "ingreso_total": round(total_ppa + ingreso_bolsa, 2),
            "emitidas": sum(1 for g in por_factura.values() if g["emitida"]),
            "facturas": len(por_factura),
        },
        "lineas": lineas,
        "por_codigo_sic": _agrupar_por_sic(facturables),
        # Las que tienen orden manual van primero, en ese orden; el resto por
        # valor descendente.
        "por_factura": sorted(
            por_factura.values(),
            key=lambda f: (
                f["orden"] is None,
                f["orden"] if f["orden"] is not None else 0,
                -f["facturacion"],
            ),
        ),
    }


def _agrupar_por_sic(facturables) -> list[dict]:
    por_sic: dict = {}
    for linea in facturables:
        clave = linea["comprador"] or "—"
        grupo = por_sic.setdefault(clave, {
            "comprador": clave, "kwh": 0.0, "facturacion": 0.0, "contratos": 0,
        })
        grupo["kwh"] += linea["kwh"]
        grupo["facturacion"] += linea["facturacion"]
        grupo["contratos"] += 1
    for grupo in por_sic.values():
        grupo["kwh"] = round(grupo["kwh"], 2)
        grupo["facturacion"] = round(grupo["facturacion"], 2)
    return sorted(por_sic.values(), key=lambda g: -g["facturacion"])


def _grupo_vacio(nombre, linea, sin_ppa=False) -> dict:
    return {
        "factura": nombre,
        "ppa": (linea["ppa"] or "—") if not sin_ppa else None,
        "comprador": linea["comprador"],
        "contratos": 0,
        "kwh": 0.0,
        "tarifa_base": linea["tarifa_base"] if not sin_ppa else None,
        "ipp_base": linea["ipp_base"] if not sin_ppa else None,
        "tarifa_indexada": linea["tarifa_indexada"] if not sin_ppa else None,
        "facturacion": 0.0,
        "personalizada": False,
        "sin_ppa": sin_ppa,
        "proyectos": [],
        "periodo_ipp_base": linea["periodo_ipp_base"] if not sin_ppa else None,
        "_tarifas": set(), "_numeros": [], "_sic": [],
        "_dias": set(), "_fmin": None, "_fmax": None,
    }


def _acumular_ventana(grupo, linea):
    if linea.get("dias"):
        grupo["_dias"].add(linea["dias"])
    for clave, extremo in (("_fmin", min), ("_fmax", max)):
        campo = "fecha_min" if clave == "_fmin" else "fecha_max"
        if linea.get(campo):
            actual = grupo[clave]
            grupo[clave] = (
                linea[campo] if actual is None else extremo(actual, linea[campo])
            )
    if linea["contrato"] and linea["contrato"] not in grupo["_sic"]:
        grupo["_sic"].append(linea["contrato"])


def _agrupar_por_factura(facturables, agrupaciones) -> dict:
    """Réplica de la hoja «Facturación» del Excel: una fila por factura.

    Un contrato con agrupación PARCIAL (porcentaje) reparte su kWh y su valor:
    el porcentaje va a la factura nombrada y el resto queda en el PPA, con la
    misma tarifa en las dos partes.
    """
    por_factura: dict = {}
    for linea in facturables:
        agrupacion = agrupaciones.get(linea["contrato"])
        nombre_ppa = linea["ppa"] or "—"
        for nombre, fraccion, porcentaje in contribuciones(agrupacion, nombre_ppa):
            es_personalizada = bool(
                agrupacion and agrupacion[0] and nombre == agrupacion[0]
            )
            grupo = por_factura.setdefault(nombre, _grupo_vacio(nombre, linea))
            _acumular_ventana(grupo, linea)

            if (
                linea["numero_contrato"]
                and linea["numero_contrato"] not in grupo["_numeros"]
            ):
                grupo["_numeros"].append(linea["numero_contrato"])

            kwh = round(linea["kwh"] * fraccion, 2)
            valor = round(linea["facturacion"] * fraccion, 2)
            grupo["kwh"] += kwh
            grupo["facturacion"] += valor
            grupo["contratos"] += 1
            if es_personalizada:
                grupo["personalizada"] = True
            if linea["tarifa_indexada"] is not None:
                grupo["_tarifas"].add(linea["tarifa_indexada"])
            grupo["proyectos"].append({
                "proyecto_id": linea["proyecto_id"],
                "proyecto": linea["proyecto"],
                "contrato": linea["contrato"],
                "ppa": linea["ppa"],
                "tarifa_indexada": linea["tarifa_indexada"],
                "kwh": kwh, "facturacion": valor,
                "porcentaje": porcentaje, "asignada": es_personalizada,
            })
    return por_factura


def _agregar_sin_ppa(por_factura: dict, lineas, bolsa) -> None:
    """Los contratos SIN PPA (UNGC / bolsa) también salen como factura.

    XM sí los factura, así que se muestran agrupados por comprador para que la
    energía total cuadre con la de XM. No tienen tarifa PPA: se valorizan a
    precio de bolsa si está cargado, y si no queda en cero.
    """
    for linea in lineas:
        if linea["estado"] != "sin_ppa":
            continue
        nombre = f'{linea["comprador"] or "Sin PPA"} (sin PPA)'
        grupo = por_factura.setdefault(
            nombre, _grupo_vacio(nombre, linea, sin_ppa=True)
        )
        _acumular_ventana(grupo, linea)

        valor = round(linea["kwh"] * bolsa, 2) if bolsa else 0.0
        grupo["tarifa_indexada"] = bolsa
        grupo["kwh"] += linea["kwh"]
        grupo["facturacion"] += valor
        grupo["contratos"] += 1
        grupo["proyectos"].append({
            "proyecto_id": linea["proyecto_id"],
            "proyecto": linea["proyecto"],
            "contrato": linea["contrato"], "ppa": None,
            "tarifa_indexada": bolsa, "kwh": linea["kwh"],
            "facturacion": valor, "porcentaje": None, "asignada": False,
        })


def _completar_facturas(por_factura: dict, per: str, ipp) -> None:
    """Redondea, marca la tarifa mixta, pega el estado de emisión y el mensaje."""
    orden_manual = dict(
        fa_models.FacturaOrden.objects.values_list("nombre", "orden")
    )
    emitidas = {
        e.nombre: e
        for e in fa_models.FacturaEmitida.objects.filter(periodo=per)
    }

    for grupo in por_factura.values():
        grupo["kwh"] = round(grupo["kwh"], 2)
        grupo["facturacion"] = round(grupo["facturacion"], 2)

        # Si la factura mezcla PPAs con tarifas distintas no hay UNA tarifa: se
        # marca mixta y la UI muestra «varía».
        grupo["tarifa_mixta"] = len(grupo["_tarifas"]) > 1
        if grupo["tarifa_mixta"]:
            grupo["tarifa_indexada"] = None
        grupo.pop("_tarifas", None)

        emitida = emitidas.get(grupo["factura"])
        grupo["emitida"] = emitida is not None
        grupo["emitida_por"] = emitida.emitida_por if emitida else None
        grupo["emitida_at"] = (
            emitida.emitida_at.isoformat()
            if emitida and emitida.emitida_at else None
        )
        grupo["numero_factura"] = emitida.numero_factura if emitida else None
        grupo["orden"] = orden_manual.get(grupo["factura"])
        grupo["numeros_contrato"] = grupo.pop("_numeros")
        grupo["contratos_sic"] = grupo.pop("_sic")

        dias = grupo.pop("_dias", set())
        # Máximo entre los contratos: comparten el archivo de despacho.
        grupo["dias"] = max(dias) if dias else None
        fmin, fmax = grupo.pop("_fmin", None), grupo.pop("_fmax", None)
        grupo["fecha_min"] = fmin.isoformat() if fmin else None
        grupo["fecha_max"] = fmax.isoformat() if fmax else None

        # El mensaje se arma acá y no en el frontend para que el formato viva en
        # un solo sitio, fijado por prueba.
        detalle: dict = {}
        for proyecto in grupo["proyectos"]:
            codigo = proyecto["contrato"]
            detalle[codigo] = round(
                detalle.get(codigo, 0.0) + (proyecto["kwh"] or 0.0), 2
            )
        grupo["mensaje"] = construir_mensaje(
            numeros_contrato=grupo["numeros_contrato"],
            periodo=per,
            kwh=grupo["kwh"],
            contratos_sic=grupo["contratos_sic"],
            contratos_detalle=[
                {"contrato": c, "kwh": k} for c, k in detalle.items()
            ],
            tarifa_base=grupo["tarifa_base"],
            ipp_base=grupo["ipp_base"],
            periodo_ipp_base=grupo["periodo_ipp_base"],
            ipp_mes=ipp,
            dias=grupo["dias"],
            fecha_min=grupo["fecha_min"],
            fecha_max=grupo["fecha_max"],
        )
