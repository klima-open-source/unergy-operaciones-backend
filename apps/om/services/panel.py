"""El panel O&M mensual: qué se factura de cada contrato de mantenimiento.

El cálculo en sí es puro y vive en `calculadora.py` (portado tal cual). Acá está
lo que necesita la base: reunir las tasas de IPC, las selecciones del mes y los
contratos, y armar las filas.
"""

from datetime import date
from pathlib import Path

from apps.contratos import models as ct_models
from apps.om import models as om_models
from apps.om.services.calculadora import calcular_proyecto, serie_indexacion
from apps.proyectos import models as py_models

SERVICIO_OM = "mantenimiento"


def tasas_ipc() -> dict[int, float]:
    return {
        t.año: float(t.tasa) for t in om_models.OmIpcTasa.objects.all()
    }


def _contrato_por_proyecto() -> dict[int, object]:
    """El contrato de mantenimiento de cada proyecto (el primero si hay varios)."""
    contratos: dict[int, object] = {}
    consulta = (
        ct_models.ContratoServicio.objects
        .filter(servicio_aplica=SERVICIO_OM, proyecto__isnull=False)
        .select_related("proyecto").order_by("id")
    )
    for contrato in consulta:
        contratos.setdefault(contrato.proyecto_id, contrato)
    return contratos


def _documentos_disponibles(periodo: str) -> dict[int, str]:
    """`{contrato_id: nombre}` solo de los documentos que EXISTEN en disco.

    El registro en base puede apuntar a un archivo perdido (p. ej. subido antes
    del volumen persistente); en ese caso el ícono de descarga no debe aparecer.
    """
    disponibles = {}
    for doc in om_models.OmDocumentoProyecto.objects.filter(periodo=periodo):
        if doc.ruta_local and Path(doc.ruta_local).exists():
            disponibles[doc.contrato_id] = doc.nombre_archivo
    return disponibles


def calculo(periodo: str) -> dict:
    """Las filas del panel para el período, con su total seleccionado."""
    ipc = tasas_ipc()
    selecciones = {
        s.contrato_id: s
        for s in om_models.OmSeleccionMensual.objects.filter(periodo=periodo)
    }
    documentos = _documentos_disponibles(periodo)
    contratos = _contrato_por_proyecto()

    # Todos los proyectos EN OPERACIÓN con el servicio de operación contratado,
    # tengan o no contrato de mantenimiento.
    proyectos = py_models.Proyecto.objects.filter(
        estado="en_operacion", srv_operacion=True
    ).order_by("nombre_comercial")

    filas, total = [], 0
    for proyecto in proyectos:
        contrato = contratos.get(proyecto.id)
        if contrato is None:
            filas.append(_fila_sin_contrato(proyecto, periodo, ipc))
            continue

        fila = _fila_con_contrato(
            proyecto, contrato, periodo, ipc,
            selecciones.get(contrato.id), documentos,
        )
        filas.append(fila)
        # Solo factura si el contrato está VIGENTE: uno en trámite se ve en el
        # panel pero no suma.
        if (
            fila["estado_contrato"] == "con_contrato"
            and fila["incluido"] and fila["habilitado"]
            and fila["valor_a_facturar"]
        ):
            total += fila["valor_a_facturar"]

    return {"periodo": periodo, "filas": filas, "total_seleccionado": total}


def _fila_sin_contrato(proyecto, periodo: str, ipc: dict) -> dict:
    """En operación pero sin contrato de mantenimiento: visible, no facturable."""
    fila = calcular_proyecto(
        # Id sintético NEGATIVO para la clave del frontend; no se persiste.
        contrato_id=-proyecto.id,
        nombre_proyecto=proyecto.nombre_comercial or f"Proyecto #{proyecto.id}",
        codigo_tsf=proyecto.codigo_tsf,
        proyecto_id=proyecto.id,
        fecha_firma_contrato=None, fecha_inicio_om=None, valor_base_anual=None,
        periodo=periodo, ipc_tasas=ipc,
    )
    fila["estado_contrato"] = "sin_contrato"
    fila["aplica_este_mes"] = False
    fila["tipo_proyecto"] = proyecto.tipo_proyecto
    return fila


def _fila_con_contrato(proyecto, contrato, periodo, ipc, seleccion, documentos):
    fila = calcular_proyecto(
        contrato_id=contrato.id,
        nombre_proyecto=(
            proyecto.nombre_comercial or contrato.prestador_nombre
            or f"Contrato #{contrato.id}"
        ),
        codigo_tsf=proyecto.codigo_tsf,
        proyecto_id=proyecto.id,
        fecha_firma_contrato=contrato.fecha_firma_contrato,
        # Fecha base de indexación = inicio de O&M: la columna dedicada o, si
        # falta, la «Fecha de inicio O&M» que edita el diálogo.
        fecha_inicio_om=contrato.fecha_inicio_om or contrato.fecha_inicio,
        valor_base_anual=(
            float(contrato.tarifa_base) if contrato.tarifa_base else None
        ),
        periodo=periodo,
        ipc_tasas=ipc,
        incluido=(seleccion.incluido if seleccion else True),
        facturado=(seleccion.facturado if seleccion else False),
        valor_manual=_valor(seleccion, "valor_manual"),
        valor_congelado=_entero(seleccion, "valor_facturado_congelado"),
        periodicidad=contrato.periodicidad_pago,
    )
    fila["estado_contrato"] = (
        "con_contrato" if contrato.estado == "firmado" else "en_tramite"
    )
    fila["tipo_proyecto"] = proyecto.tipo_proyecto
    fila["motivo_exclusion"] = seleccion.motivo_exclusion if seleccion else None
    fila["documento_disponible"] = contrato.id in documentos
    fila["documento_nombre"] = documentos.get(contrato.id)
    return fila


def _valor(seleccion, campo):
    valor = getattr(seleccion, campo, None) if seleccion else None
    return float(valor) if valor is not None else None


def _entero(seleccion, campo):
    valor = getattr(seleccion, campo, None) if seleccion else None
    return int(valor) if valor is not None else None


def valor_de_proyecto(proyecto_id: int, periodo: str) -> float | None:
    """El valor O&M mensual de UN proyecto, con el MISMO cálculo que el panel.

    Fuente única para que el Panel Contable no duplique la lógica. `None` si el
    proyecto no tiene contrato de mantenimiento; `0` si el contrato no está
    vigente o la fila queda deshabilitada o excluida.
    """
    contrato = (
        ct_models.ContratoServicio.objects
        .filter(servicio_aplica=SERVICIO_OM, proyecto_id=proyecto_id)
        .order_by("id").first()
    )
    if contrato is None:
        return None
    if contrato.estado != "firmado":
        return 0.0

    proyecto = py_models.Proyecto.objects.filter(pk=proyecto_id).first()
    seleccion = om_models.OmSeleccionMensual.objects.filter(
        contrato_id=contrato.id, periodo=periodo
    ).first()

    fila = calcular_proyecto(
        contrato_id=contrato.id,
        nombre_proyecto=(proyecto.nombre_comercial if proyecto else None) or "",
        fecha_firma_contrato=contrato.fecha_firma_contrato,
        fecha_inicio_om=contrato.fecha_inicio_om or contrato.fecha_inicio,
        valor_base_anual=(
            float(contrato.tarifa_base) if contrato.tarifa_base else None
        ),
        periodo=periodo,
        ipc_tasas=tasas_ipc(),
        incluido=(seleccion.incluido if seleccion else True),
        facturado=(seleccion.facturado if seleccion else False),
        valor_manual=_valor(seleccion, "valor_manual"),
        valor_congelado=_entero(seleccion, "valor_facturado_congelado"),
        periodicidad=contrato.periodicidad_pago,
    )
    if not fila.get("habilitado") or not fila.get("incluido"):
        return 0.0
    return float(fila.get("valor_a_facturar") or 0)


def indexacion(contrato) -> dict:
    """Serie de indexación anual y mensual de un contrato.

    Aniversario desde la fecha de inicio de O&M más el IPC de cada año, y solo
    los aniversarios YA cumplidos a hoy: proyectar hacia adelante daría un valor
    que aún no se puede facturar.
    """
    hoy = date.today()
    serie = serie_indexacion(
        contrato.fecha_inicio_om or contrato.fecha_inicio,
        float(contrato.tarifa_base) if contrato.tarifa_base else None,
        tasas_ipc(), hoy.year, hoy.month,
    )
    return {
        "anual": [
            {
                "anio": f["anio"], "ipc_aplicado": f["ipc_aplicado"],
                "valor": f["valor_anual"],
            }
            for f in serie
        ],
        "mensual": [
            {
                "anio": f["anio"], "ipc_aplicado": f["ipc_aplicado"],
                "valor": f["valor_mensual"],
            }
            for f in serie
        ],
    }


def congelar_valor(seleccion, contrato) -> None:
    """Fija el valor calculado en el momento de marcar como facturado.

    Sin congelarlo, un arreglo posterior en la indexación cambiaría el valor de
    un mes que ya se facturó.
    """
    proyecto = contrato.proyecto
    fila = calcular_proyecto(
        contrato_id=contrato.id,
        nombre_proyecto=(
            (proyecto.nombre_comercial if proyecto else None)
            or contrato.prestador_nombre or f"Contrato #{contrato.id}"
        ),
        fecha_firma_contrato=contrato.fecha_firma_contrato,
        fecha_inicio_om=contrato.fecha_inicio_om or contrato.fecha_inicio,
        valor_base_anual=(
            float(contrato.tarifa_base) if contrato.tarifa_base else None
        ),
        periodo=seleccion.periodo,
        ipc_tasas=tasas_ipc(),
        valor_manual=_valor(seleccion, "valor_manual"),
    )
    seleccion.valor_facturado_congelado = fila["valor_a_facturar"]
