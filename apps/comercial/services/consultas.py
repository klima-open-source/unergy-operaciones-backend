"""Los tres listados del CRM: oportunidades, ofertas y PPAs del pipeline.

Puerto de `list_oportunidades`, `list_ofertas_todas` y `list_ppas_del_pipeline`
de `app/api/v1/comercial.py`.

**La unidad del CRM es la OFERTA, no el cliente.** Por eso la lista principal de
/comercial es plana sobre ofertas, y la de oportunidades devuelve `etapas` —un
conteo por etapa de sus ofertas— en vez de un estado único. La alerta también es
de la oferta: cuenta desde que ENTRÓ a su etapa actual, así que una firmada deja
de alertar aunque su hermana lleve meses sin respuesta.

Todo se resuelve por lotes: un número FIJO de consultas sin importar cuántas
filas entren. La vista principal carga todas las ofertas de una.
"""

from __future__ import annotations

from datetime import date

from django.db.models import Count, Max, Q

from apps.clientes.models import Cliente
from apps.comercial.models import (
    Oportunidad, OportunidadGestion, OportunidadOferta, OportunidadOfertaProyecto,
)
from apps.comercial.services.pipeline import (
    calcular_alerta, col_now, contexto_ficha, ficha_operativa,
)
from apps.comercial.services.salidas import (
    ALERTA_DIAS, norm_codigo, op_base_out, oferta_out, proyecto_out, valor,
)
from apps.proyectos.models import Proyecto


def fichas(ofertas) -> dict[int, dict]:
    """`{oferta_id: ficha}` con la precarga por lotes hecha UNA sola vez."""
    ofertas = list(ofertas)
    ctx = contexto_ficha(ofertas)
    return {o.id: ficha_operativa(o, **ctx[o.id]) for o in ofertas}


def plantas_de_ofertas(ofertas) -> dict[int, list]:
    """`{oferta_id: [plantas]}` en dos consultas, no en dos por oferta.

    Con fallback al `proyecto_id` único para las ofertas sin filas en la M2M —
    mismo criterio que usa `/firmar`, para que la UI muestre exactamente las
    plantas que se van a firmar.
    """
    ofertas = list(ofertas)
    ids = [o.id for o in ofertas]
    if not ids:
        return {}

    por_oferta: dict[int, list[int]] = {}
    for oferta_id, proyecto_id in OportunidadOfertaProyecto.objects.filter(
        oferta_id__in=ids
    ).values_list("oferta_id", "proyecto_id"):
        por_oferta.setdefault(oferta_id, []).append(proyecto_id)
    for o in ofertas:
        if o.id not in por_oferta and o.proyecto_id:
            por_oferta[o.id] = [o.proyecto_id]

    todos = {pid for lista in por_oferta.values() for pid in lista}
    if not todos:
        return {}
    proyectos = {
        p.id: p for p in Proyecto.objects
        .filter(id__in=todos, deleted_at__isnull=True)
        .select_related("operador_red")
        .prefetch_related("fronteras__operador_red")
    }
    return {
        oid: [proyecto_out(proyectos[pid]) for pid in lista if pid in proyectos]
        for oid, lista in por_oferta.items()
    }


def oferta_completa(oferta) -> dict:
    """La respuesta de UNA oferta (ficha + plantas).

    La usan los endpoints que devuelven la fila recién escrita, para que el front
    no tenga que recargar la lista entera después de cada edición.
    """
    return oferta_out(
        oferta,
        fichas([oferta])[oferta.id],
        plantas_de_ofertas([oferta]).get(oferta.id, []),
    )


class UltimaGestion:
    """La última gestión relevante PARA UNA OFERTA: la más reciente entre las
    suyas (`oferta_id` = ella) y las del cliente (`oferta_id` NULL, que cuentan
    para todas sus ofertas).

    Se agrega en Python y no en SQL porque el "o es de esta oferta o no es de
    ninguna" no cabe en un GROUP BY simple, y la bitácora es chica. Antes se
    agrupaba solo por `oportunidad_id`: registrar la llamada por Margaritas 1
    apagaba la alerta de Margaritas 2, que seguía muda.
    """

    def __init__(self, del_cliente: dict, de_la_oferta: dict):
        self._cliente = del_cliente
        self._oferta = de_la_oferta

    def para(self, oportunidad_id: int, oferta_id: int):
        candidatas = [
            f for f in (self._cliente.get(oportunidad_id), self._oferta.get(oferta_id))
            if f is not None
        ]
        return max(candidatas) if candidatas else None


def ultima_gestion() -> UltimaGestion:
    filas = (
        OportunidadGestion.objects
        .values("oportunidad_id", "oferta_id")
        .annotate(fecha=Max("fecha"))
    )
    del_cliente: dict = {}
    de_la_oferta: dict = {}
    for f in filas:
        if f["fecha"] is None:
            continue
        if f["oferta_id"] is None:
            previa = del_cliente.get(f["oportunidad_id"])
            if previa is None or f["fecha"] > previa:
                del_cliente[f["oportunidad_id"]] = f["fecha"]
        else:
            de_la_oferta[f["oferta_id"]] = f["fecha"]
    return UltimaGestion(del_cliente, de_la_oferta)


def listar_oportunidades(estado=None, tipo_servicio=None, cliente_id=None,
                         q=None, solo_alerta=False) -> list[dict]:
    qs = (
        Oportunidad.objects
        .filter(deleted_at__isnull=True, cliente__deleted_at__isnull=True)
        .select_related("cliente")
    )
    if estado:
        # El estado ya no es del cliente: se filtra por tener ≥1 oferta en esa etapa.
        qs = qs.filter(
            id__in=OportunidadOferta.objects.filter(estado=estado)
            .values("oportunidad_id")
        )
    if tipo_servicio:
        qs = qs.filter(
            id__in=OportunidadOferta.objects.filter(tipo=tipo_servicio)
            .values("oportunidad_id")
        )
    if cliente_id:
        qs = qs.filter(cliente_id=cliente_id)
    if q:
        aguja = q.strip()
        qs = qs.filter(
            Q(nombre__icontains=aguja) | Q(cliente__razon_social_nombre__icontains=aguja)
        )

    ahora = col_now()
    oportunidades = list(qs.order_by("-updated_at"))
    op_ids = [op.id for op in oportunidades]
    if not op_ids:
        return []

    # Para la LISTA de oportunidades la última gestión es la más reciente de
    # TODAS las del cliente, de oferta o no — no la de una oferta puntual (eso lo
    # necesita el listado de ofertas, ver `ultima_gestion`).
    ultima_por_op = {
        f["oportunidad_id"]: f["fecha"]
        for f in OportunidadGestion.objects
        .filter(oportunidad_id__in=op_ids)
        .values("oportunidad_id")
        .annotate(fecha=Max("fecha"))
    }

    # Conteo de ofertas por (oportunidad, tipo), en una sola consulta.
    resumen_por_op: dict = {}
    for f in OportunidadOferta.objects.filter(
        oportunidad_id__in=op_ids
    ).values("oportunidad_id", "tipo").annotate(n=Count("id")):
        resumen_por_op.setdefault(f["oportunidad_id"], {})[f["tipo"]] = int(f["n"])

    # La oferta "principal" de cada oportunidad (la más reciente por fecha/id):
    # de ahí sale el código de seguimiento de la fila. La oportunidad HEREDA la
    # identidad de su oferta líder; todas comparten la familia OP.*.
    lead_por_op: dict = {}
    num_ofertas_por_op: dict = {}
    estados_por_op: dict = {}
    oferta_a_op: dict = {}
    proyecto_de_oferta: dict = {}
    for f in OportunidadOferta.objects.filter(oportunidad_id__in=op_ids).values(
        "oportunidad_id", "numero_oferta", "planta_nombre", "tipo",
        "proyecto_id", "fecha_oferta", "id", "estado", "estado_desde",
    ):
        oid, ofid = f["oportunidad_id"], f["id"]
        num_ofertas_por_op[oid] = num_ofertas_por_op.get(oid, 0) + 1
        estados_por_op.setdefault(oid, []).append((f["estado"], f["estado_desde"]))
        oferta_a_op[ofid] = oid
        proyecto_de_oferta[ofid] = f["proyecto_id"]
        clave = (f["fecha_oferta"] or date.min, ofid)
        previa = lead_por_op.get(oid)
        if previa is None or clave > previa[0]:
            lead_por_op[oid] = (clave, {
                "codigo_seguimiento": norm_codigo(f["numero_oferta"]),
                "planta_nombre": f["planta_nombre"],
                "tipo": f["tipo"],
                "proyecto_id": f["proyecto_id"],
            })

    # Plantas vinculadas por oportunidad: la unión de las de todas sus ofertas,
    # con el mismo fallback a `proyecto_id` que usa `/firmar`. Antes salía de
    # `Proyecto.oportunidad_id`, una columna que nunca se llenaba (0/188), y eso
    # dejaba `num_proyectos` y `capacidad_total_kwp` en 0 para TODA oportunidad.
    proyectos_por_op: dict[int, set[int]] = {}
    ofertas_con_m2m = set()
    for ofid, pid in OportunidadOfertaProyecto.objects.filter(
        oferta_id__in=oferta_a_op
    ).values_list("oferta_id", "proyecto_id"):
        ofertas_con_m2m.add(ofid)
        oid = oferta_a_op.get(ofid)
        if oid is not None and pid is not None:
            proyectos_por_op.setdefault(oid, set()).add(pid)
    for ofid, oid in oferta_a_op.items():
        if ofid not in ofertas_con_m2m and proyecto_de_oferta.get(ofid):
            proyectos_por_op.setdefault(oid, set()).add(proyecto_de_oferta[ofid])

    todos_los_pid = {pid for s in proyectos_por_op.values() for pid in s}
    kwp_por_pid = {
        pid: float(kwp) if kwp is not None else 0.0
        for pid, kwp in Proyecto.objects
        .filter(id__in=todos_los_pid, deleted_at__isnull=True)
        .values_list("id", "potencia_ac_kw")
    } if todos_los_pid else {}

    salida = []
    for op in oportunidades:
        fila = op_base_out(
            op, op.cliente, ultima_por_op.get(op.id), ahora, estados_por_op.get(op.id),
        )
        pids = proyectos_por_op.get(op.id, set()) & kwp_por_pid.keys()
        fila["num_proyectos"] = len(pids)
        fila["capacidad_total_kwp"] = sum(kwp_por_pid[p] for p in pids)
        fila["resumen_ofertas"] = resumen_por_op.get(op.id, {})
        fila["num_ofertas"] = num_ofertas_por_op.get(op.id, 0)
        lead = lead_por_op.get(op.id)
        fila["oferta_principal"] = lead[1] if lead else None
        # El código de la fila: el de la oferta líder o, si no hay ofertas, el
        # consecutivo propio de la oportunidad (también normalizado).
        fila["codigo_seguimiento"] = (
            lead[1]["codigo_seguimiento"] if lead else norm_codigo(op.numero_oferta)
        )
        if solo_alerta and not fila["alerta"]:
            continue
        salida.append(fila)
    return salida


def listar_ofertas(tipo=None, estado=None, resultado=None, q=None,
                   solo_alerta=False) -> list[dict]:
    """Lista PLANA de todas las ofertas: la fuente de la vista principal.

    Cada fila trae su código de seguimiento, tipo, planta, resultado y —heredados
    de su oportunidad— el cliente y la alerta.
    """
    qs = (
        OportunidadOferta.objects
        .filter(
            oportunidad__deleted_at__isnull=True,
            oportunidad__cliente__deleted_at__isnull=True,
        )
        .select_related("oportunidad__cliente")
    )
    for campo, valor_ in (("tipo", tipo), ("estado", estado), ("resultado", resultado)):
        if valor_:
            qs = qs.filter(**{campo: valor_})
    if q:
        aguja = q.strip()
        qs = qs.filter(
            Q(numero_oferta__icontains=aguja)
            | Q(planta_nombre__icontains=aguja)
            | Q(oportunidad__cliente__razon_social_nombre__icontains=aguja)
            | Q(oportunidad__nombre__icontains=aguja)
        )

    ahora = col_now()
    ofertas = list(qs.order_by("-updated_at", "-id"))
    if not ofertas:
        return []

    todas_las_fichas = fichas(ofertas)
    todas_las_plantas = plantas_de_ofertas(ofertas)
    gestiones = ultima_gestion()

    salida = []
    for of in ofertas:
        op = of.oportunidad
        cliente = op.cliente
        # La alerta es de la OFERTA: cuenta desde que entró a su etapa actual, no
        # desde que el cliente cambió de estado.
        dias, alerta = calcular_alerta(
            valor(of.estado), of.estado_desde or op.estado_desde,
            gestiones.para(op.id, of.id), ALERTA_DIAS, ahora,
        )
        if solo_alerta and not alerta:
            continue
        fila = oferta_out(of, todas_las_fichas[of.id], todas_las_plantas.get(of.id, []))
        fila.update({
            "cliente_id": op.cliente_id,
            "cliente_razon_social": cliente.razon_social_nombre,
            "cliente_nit": cliente.nit_cedula,
            "oportunidad_nombre": op.nombre or cliente.razon_social_nombre,
            "dias_sin_respuesta": dias,
            "alerta": alerta,
        })
        salida.append(fila)
    return salida
