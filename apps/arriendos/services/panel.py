"""El panel de Arriendos mensual — espejo del de O&M.

Itera `Proyecto` (en operación) más su `ContratoServicio` de arriendo, sin tabla
intermedia. Todo proyecto en operación aparece, tenga o no contrato de arriendo:
`sin_contrato` significa visible pero no facturable, para poder configurarlo a
mano desde Operación.

Diferencia clave con O&M: un contrato de arriendo puede tener VARIOS
arrendadores, y cada uno factura su parte con su propio IVA.
"""

from datetime import date
from types import SimpleNamespace

from apps.arriendos import models as ar_models
from apps.arriendos.services.calculadora import (
    calcular_arriendo, calcular_iva, serie_indexacion,
)
from apps.contratos import models as ct_models
from apps.proyectos import models as py_models

SERVICIO = "arriendo"
MESES_DEL_ANIO = 12


def tasas_ipc() -> dict[int, float]:
    return {t.año: float(t.tasa) for t in ar_models.ArrIpcTasa.objects.all()}


def _canon_mensual(valor_anual) -> float | None:
    """El valor base se guarda ANUAL; el panel factura mensual."""
    return float(valor_anual) / MESES_DEL_ANIO if valor_anual is not None else None


def _contrato_por_proyecto() -> dict[int, object]:
    contratos: dict[int, object] = {}
    consulta = (
        ct_models.ContratoServicio.objects
        .filter(servicio_aplica=SERVICIO, proyecto__isnull=False)
        .order_by("id")
    )
    for contrato in consulta:
        contratos.setdefault(contrato.proyecto_id, contrato)
    return contratos


def arrendadores_de(contrato) -> list:
    """Los arrendadores activos del contrato, o uno sintético si no hay ninguno.

    El sintético usa el prestador y la tarifa del contrato: así una planta cuyo
    arrendador nunca se dio de alta sigue apareciendo con su canon en vez de
    desaparecer del panel.
    """
    activos = list(
        ar_models.ArrArrendador.objects
        .filter(contrato=contrato, activo=True).order_by("id")
    )
    if activos:
        return activos
    return [SimpleNamespace(
        id=None,
        nombre=contrato.prestador_nombre or "Arrendador",
        valor_base=contrato.tarifa_base,
        responsable_iva=contrato.responsable_iva,
        anticipo_pagado_desde=None,
        anticipo_pagado_hasta=None,
        observaciones=None,
    )]


def calculo(periodo: str) -> dict:
    ipc = tasas_ipc()
    selecciones = {
        s.arr_arrendador_id: s
        for s in ar_models.ArrSeleccionMensual.objects.filter(periodo=periodo)
    }
    contratos = _contrato_por_proyecto()
    proyectos = py_models.Proyecto.objects.filter(
        estado="en_operacion", srv_operacion=True
    ).order_by("nombre_comercial")

    filas, total = [], 0
    for proyecto in proyectos:
        contrato = contratos.get(proyecto.id)
        if contrato is None:
            filas.append(_fila_sin_contrato(proyecto, periodo, ipc))
            continue

        estado = "con_contrato" if contrato.estado == "firmado" else "en_tramite"
        for arrendador in arrendadores_de(contrato):
            fila = _fila(
                proyecto, contrato, arrendador, periodo, ipc,
                selecciones.get(arrendador.id), estado,
            )
            filas.append(fila)
            if (
                estado == "con_contrato" and fila["incluido"]
                and fila["habilitado"] and fila["aplica_este_mes"]
                and fila["canon_a_facturar"]
            ):
                total += fila["canon_a_facturar"]

    return {"periodo": periodo, "filas": filas, "total_seleccionado": total}


def _fila_sin_contrato(proyecto, periodo: str, ipc: dict) -> dict:
    fila = calcular_arriendo(
        # Id sintético NEGATIVO: el frontend lo usa como clave y no se persiste.
        proyecto_id=-proyecto.id,
        nombre=proyecto.nombre_comercial or f"Proyecto #{proyecto.id}",
        codigo=proyecto.codigo_tsf,
        fecha_firma_contrato=None, valor_base=None,
        periodo=periodo, ipc_tasas=ipc,
    )
    fila.update({
        "iva_calculado": None,
        "nombre_arrendador": None,
        "motivo_exclusion": None,
        "tipo_proyecto": proyecto.tipo_proyecto,
        "estado_contrato": "sin_contrato",
        "aplica_este_mes": False,
        "proyecto_id": proyecto.id,
    })
    return fila


def _fila(proyecto, contrato, arrendador, periodo, ipc, seleccion, estado) -> dict:
    fila = calcular_arriendo(
        proyecto_id=arrendador.id,
        nombre=proyecto.nombre_comercial,
        codigo=proyecto.codigo_tsf,
        fecha_firma_contrato=contrato.fecha_firma_contrato,
        valor_base=_canon_mensual(arrendador.valor_base),
        periodo=periodo, ipc_tasas=ipc,
        incluido=(seleccion.incluido if seleccion else True),
        facturado=(seleccion.facturado if seleccion else False),
        valor_congelado=_congelado(seleccion),
        periodicidad=contrato.periodicidad_pago,
        anticipo_pagado_desde=getattr(arrendador, "anticipo_pagado_desde", None),
        anticipo_pagado_hasta=getattr(arrendador, "anticipo_pagado_hasta", None),
    )
    fila.update({
        # El IVA es POR ARRENDADOR: dos arrendadores del mismo contrato pueden
        # tener responsabilidad distinta.
        "iva_calculado": calcular_iva(
            fila["canon_a_facturar"], arrendador.responsable_iva
        ),
        "nombre_arrendador": arrendador.nombre,
        "motivo_exclusion": seleccion.motivo_exclusion if seleccion else None,
        "tipo_proyecto": proyecto.tipo_proyecto,
        "estado_contrato": estado,
        "proyecto_id": contrato.proyecto_id,
        "anticipo_pagado_desde": getattr(arrendador, "anticipo_pagado_desde", None),
        "anticipo_pagado_hasta": getattr(arrendador, "anticipo_pagado_hasta", None),
        "observaciones_arrendador": getattr(arrendador, "observaciones", None),
    })
    return fila


def _congelado(seleccion):
    valor = getattr(seleccion, "valor_facturado_congelado", None) if seleccion else None
    return int(valor) if valor is not None else None


def valor_de_proyecto(proyecto_id: int, periodo: str) -> tuple[float, float] | None:
    """`(canon, iva)` mensual de UN proyecto, con el MISMO cálculo que el panel.

    Fuente única para el Panel Contable. `None` si no hay contrato de arriendo;
    `(0, 0)` si el contrato no está vigente.
    """
    contrato = (
        ct_models.ContratoServicio.objects
        .filter(servicio_aplica=SERVICIO, proyecto_id=proyecto_id)
        .order_by("id").first()
    )
    if contrato is None:
        return None
    if contrato.estado != "firmado":
        return (0.0, 0.0)

    proyecto = py_models.Proyecto.objects.filter(pk=proyecto_id).first()
    ipc = tasas_ipc()
    selecciones = {
        s.arr_arrendador_id: s
        for s in ar_models.ArrSeleccionMensual.objects.filter(periodo=periodo)
    }

    canon = iva = 0.0
    for arrendador in arrendadores_de(contrato):
        seleccion = selecciones.get(arrendador.id)
        fila = calcular_arriendo(
            proyecto_id=arrendador.id,
            nombre=(proyecto.nombre_comercial if proyecto else None),
            codigo=getattr(proyecto, "codigo_tsf", None),
            fecha_firma_contrato=contrato.fecha_firma_contrato,
            valor_base=_canon_mensual(arrendador.valor_base),
            periodo=periodo, ipc_tasas=ipc,
            incluido=(seleccion.incluido if seleccion else True),
            facturado=(seleccion.facturado if seleccion else False),
            valor_congelado=_congelado(seleccion),
            periodicidad=contrato.periodicidad_pago,
            anticipo_pagado_desde=getattr(arrendador, "anticipo_pagado_desde", None),
            anticipo_pagado_hasta=getattr(arrendador, "anticipo_pagado_hasta", None),
        )
        if (
            fila.get("habilitado") and fila.get("incluido")
            and fila.get("aplica_este_mes")
        ):
            canon += float(fila.get("canon_a_facturar") or 0)
            iva += float(calcular_iva(
                fila.get("canon_a_facturar"),
                getattr(arrendador, "responsable_iva", False),
            ) or 0)
    return (canon, iva)


def indexacion(contrato, arrendador=None) -> dict:
    """Serie de indexación anual y mensual de un contrato de arriendo.

    Año CALENDARIO (1 de enero) usando solo el año de la fecha de firma — a
    diferencia de O&M, que va por aniversario. La fecha base y la periodicidad
    son del contrato (compartidas); con `arrendador`, el valor base es el de ESE
    arrendador en vez de la tarifa del contrato.
    """
    base = (
        _canon_mensual(arrendador.valor_base) if arrendador is not None
        else _canon_mensual(contrato.tarifa_base)
    )
    hoy = date.today()
    serie = serie_indexacion(
        contrato.fecha_firma_contrato, base, tasas_ipc(), hoy.year, hoy.month
    )
    return {
        "anual": [
            {"anio": f["anio"], "ipc_aplicado": f["ipc_aplicado"],
             "valor": f["valor_anual"]}
            for f in serie
        ],
        "mensual": [
            {"anio": f["anio"], "ipc_aplicado": f["ipc_aplicado"],
             "valor": f["valor_mensual"]}
            for f in serie
        ],
    }


def congelar_canon(seleccion, arrendador) -> None:
    """Fija el canon calculado al marcar como facturado."""
    contrato = ct_models.ContratoServicio.objects.filter(
        pk=arrendador.contrato_id
    ).first()
    fila = calcular_arriendo(
        proyecto_id=arrendador.id, nombre=arrendador.nombre, codigo=None,
        fecha_firma_contrato=(
            contrato.fecha_firma_contrato if contrato else None
        ),
        valor_base=_canon_mensual(arrendador.valor_base),
        periodo=seleccion.periodo, ipc_tasas=tasas_ipc(),
        periodicidad=contrato.periodicidad_pago if contrato else None,
    )
    seleccion.valor_facturado_congelado = fila["canon_a_facturar"]
