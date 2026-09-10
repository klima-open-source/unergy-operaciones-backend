"""Lo que el CRM ESCRIBE: clientes, oportunidades, ofertas y la firma del PPA.

Puerto de las escrituras de `app/api/v1/comercial.py`.

**`registrar` es todo o nada a propósito.** En dos llamadas, cuando la segunda
fallaba quedaba una oportunidad SIN ofertas: la UI decía "creado" y después no
aparecía en ninguna vista, porque el tablero y la tabla se alimentan de las
ofertas.

**`firmar` es idempotente por enlace**: si la oferta ya tiene contrato responde
409 en vez de crear un segundo. Y las condiciones NO se copian a la oferta — el
contrato es la fuente única, que es lo que ya leen Cumplimiento y Liquidaciones.
"""

from __future__ import annotations

import re

from django.db import transaction
from rest_framework.exceptions import NotFound

from api.exceptions import Conflict, NoProcesable
from apps.clientes.models import Cliente, Contacto
from apps.clientes.services.gestion import buscar_duplicado, normalizar_nit
from apps.comercial.models import (
    Oportunidad, OportunidadEstadoHistorial, OportunidadOferta,
    OportunidadOfertaProyecto,
)
from apps.comercial.services.documentos import set_enlace_documento
from apps.comercial.services.pipeline import col_now, estado_a_resultado
from apps.comercial.services.salidas import _SEG_TIPO, norm_codigo, valor
from apps.comun.nombre_matching import parece_persona_juridica
from apps.fronteras.models import OperadorRed
from apps.ppa.models import PpaContrato, PpaContratoProyecto, PpaTarifa
from apps.proyectos.models import Proyecto

_RE_CONSECUTIVO = re.compile(r"No\.\s*(\d+)")

# Los dos tipos de oferta que desembocan en un PPA. Las de servicios
# (representación, CGM) van a `contratos_servicio` y no son contratos de energía.
TIPOS_ENERGIA = ("compra_energia", "comunidad_energetica")


def get_oportunidad(id: int) -> Oportunidad:
    op = Oportunidad.objects.filter(pk=id, deleted_at__isnull=True).first()
    if not op:
        raise NotFound("Oportunidad no encontrada")
    return op


def validar_operador_red(operador_red_id: int | None) -> None:
    """El FK al catálogo se valida acá y no en la base: sin esto, un id inventado
    revienta como IntegrityError 500 en vez de un 422 con mensaje."""
    if operador_red_id is None:
        return
    if not OperadorRed.objects.filter(pk=operador_red_id).exists():
        raise NoProcesable("operador_red_id no existe en el catálogo de operadores")


def _next_consecutivo() -> int:
    """El siguiente consecutivo global: el máximo NNNN visto en los códigos + 1."""
    maximo = 0
    for codigo in OportunidadOferta.objects.filter(
        numero_oferta__isnull=False
    ).values_list("numero_oferta", flat=True):
        m = _RE_CONSECUTIVO.search(codigo or "")
        if m:
            maximo = max(maximo, int(m.group(1)))
    return maximo + 1


def gen_codigo(tipo: str, fecha) -> str:
    """`OP.{SEG} No.{NNNN}-{MM}-{YYYY}` para una oferta nueva sin número."""
    ref = fecha or col_now()
    return f"OP.{_SEG_TIPO.get(tipo, 'REP')} No.{_next_consecutivo():04d}-{ref.month}-{ref.year}"


def set_plantas(oferta: OportunidadOferta, proyecto_ids: list[int]) -> None:
    """Reescribe las plantas de la oferta (M2M). Lista vacía = desvincular todas.

    Mantiene `proyecto_id` en la primera de la lista: el vinculador, la ficha
    operativa y `proyectos-operando` siguen leyendo esa columna, así que dejarla
    desincronizada haría que la oferta se viera con una planta en el drawer y con
    otra en la API de integración.
    """
    ids: list[int] = []
    for pid in proyecto_ids:
        if pid not in ids:
            ids.append(pid)
    if ids:
        existen = set(
            Proyecto.objects.filter(id__in=ids, deleted_at__isnull=True)
            .values_list("id", flat=True)
        )
        faltan = [i for i in ids if i not in existen]
        if faltan:
            raise NoProcesable(f"Proyectos inexistentes: {faltan}")

    OportunidadOfertaProyecto.objects.filter(oferta_id=oferta.id).delete()
    OportunidadOfertaProyecto.objects.bulk_create([
        OportunidadOfertaProyecto(oferta_id=oferta.id, proyecto_id=pid) for pid in ids
    ])
    oferta.proyecto_id = ids[0] if ids else None


def sumar_planta(oferta: OportunidadOferta, proyecto_id: int) -> None:
    """Pega la planta a la oferta SIN tocar las que ya tenía.

    Distinto de `set_plantas`, que reescribe la lista entera: acá se está
    agregando una planta nueva, y una oferta puede cubrir varias ("Balmora 1 y
    2"). Reescribir dejaría fuera a sus hermanas.

    `proyecto_id` solo se estampa si estaba vacía: si la oferta ya tenía una
    planta principal, esta se suma detrás y no la desplaza.
    """
    OportunidadOfertaProyecto.objects.get_or_create(
        oferta_id=oferta.id, proyecto_id=proyecto_id,
    )
    if oferta.proyecto_id is None:
        oferta.proyecto_id = proyecto_id
        oferta.save(update_fields=["proyecto_id"])


def resolver_cliente(cliente_id: int | None, cliente_nuevo: dict | None,
                     forzar_duplicado: bool) -> Cliente:
    """El cliente existente, o uno nuevo con sus contactos.

    El 409 de duplicado trae `candidato_id` a propósito: la UI ofrece "usar ese
    cliente" en vez de dejar al comercial trabado con un error rojo.
    """
    if cliente_id:
        cliente = Cliente.objects.filter(pk=cliente_id, deleted_at__isnull=True).first()
        if not cliente:
            raise NoProcesable("Cliente no encontrado")
        return cliente

    cn = cliente_nuevo or {}
    if not forzar_duplicado:
        # `nit_cedula` tiene UNIQUE en la base pero nunca se revisaba antes de
        # crear: el choque salía como 500 crudo del INSERT. Un NIT igual es
        # evidencia mucho más fuerte que un nombre parecido —prácticamente
        # certeza de que es la misma empresa—, así que se revisa PRIMERO y
        # aparte, sin pasar por el umbral de similitud.
        duplicado = None
        por_nit = False
        # Normalizado, igual que en `POST /clientes`: comparar el texto crudo
        # dejaba pasar el mismo NIT escrito con puntos o guiones, y al crear
        # dejaba una fila cruda que despues no choca con ninguna normalizada.
        nit = normalizar_nit(cn.get("nit_cedula"))
        if nit:
            duplicado = Cliente.objects.filter(
                nit_cedula=nit, deleted_at__isnull=True,
            ).first()
            por_nit = duplicado is not None
        if not duplicado:
            duplicado = buscar_duplicado(cn.get("razon_social_nombre"))
        if duplicado:
            raise Conflict({
                "mensaje": (
                    f"Ya existe un cliente con el mismo NIT/cédula: "
                    f"'{duplicado.razon_social_nombre}' (ID {duplicado.id})."
                    if por_nit else
                    f"Ya existe un cliente con un nombre muy parecido: "
                    f"'{duplicado.razon_social_nombre}' (ID {duplicado.id})."
                ),
                "duplicado_nombre": not por_nit,
                "duplicado_nit": por_nit,
                "candidato_id": duplicado.id,
                "candidato_nombre": duplicado.razon_social_nombre,
            })

    cliente = Cliente.objects.create(
        razon_social_nombre=cn.get("razon_social_nombre"),
        nit_cedula=normalizar_nit(cn.get("nit_cedula")),
        origen_tipo=cn.get("origen_tipo"),
        origen_detalle=cn.get("origen_detalle"),
        # Sugerencia, no certeza: solo se marca "juridica" cuando la razón social
        # trae un indicador reconocible (S.A.S./LTDA/E.S.P./fiduciaria/patrimonio
        # autónomo). NUNCA se marca "natural" por ausencia de esos indicadores —
        # no hay suficientes clientes reales así en la plataforma para confiar en
        # ese lado de la regla. Editable después desde la ficha.
        tipo_persona=(
            "juridica" if parece_persona_juridica(cn.get("razon_social_nombre")) else None
        ),
    )
    Contacto.objects.bulk_create([
        Contacto(
            cliente_id=cliente.id, nombre=c.get("nombre"), telefono=c.get("telefono"),
            email=c.get("email"), tipo=c.get("tipo"),
        )
        for c in (cn.get("contactos") or [])
    ])
    return cliente


def nueva_oportunidad(cliente: Cliente, nombre, notas, usuario) -> Oportunidad:
    """La oportunidad y su fila de histórico."""
    op = Oportunidad.objects.create(
        cliente_id=cliente.id,
        nombre=nombre,
        notas=notas,
        estado="oportunidad",          # SIEMPRE del lado del servidor
        estado_desde=col_now(),
        creado_por_usuario_id=getattr(usuario, "id", None),
    )
    OportunidadEstadoHistorial.objects.create(
        oportunidad_id=op.id, estado_anterior=None,
        estado_nuevo="oportunidad", usuario_id=getattr(usuario, "id", None),
    )
    return op


def nueva_oferta(oportunidad_id: int, datos: dict, usuario) -> OportunidadOferta:
    """Crea la oferta y su fila de histórico."""
    payload = dict(datos)
    # `proyecto_ids` no es columna: es la M2M, y se escribe cuando la oferta ya
    # tiene id.
    proyecto_ids = payload.pop("proyecto_ids", None)
    validar_operador_red(payload.get("operador_red_id"))
    if not payload.get("numero_oferta"):
        payload["numero_oferta"] = gen_codigo(payload["tipo"], payload.get("fecha_oferta"))
    payload["resultado"] = estado_a_resultado(payload["estado"])

    oferta = OportunidadOferta.objects.create(
        oportunidad_id=oportunidad_id, estado_desde=col_now(), **payload,
    )
    if proyecto_ids is not None:
        set_plantas(oferta, proyecto_ids)
        oferta.save(update_fields=["proyecto_id"])
    OportunidadEstadoHistorial.objects.create(
        oportunidad_id=oportunidad_id, oferta_id=oferta.id, estado_anterior=None,
        estado_nuevo=payload["estado"], usuario_id=getattr(usuario, "id", None),
    )
    return oferta


def mover_todas_las_ofertas(op: Oportunidad, estado: str, usuario) -> dict:
    """Mueve TODAS las ofertas del cliente a una etapa.

    Se conserva porque el tablero viejo arrastra la tarjeta del cliente; para
    mover UNA oferta —que es lo normal— está `mover_oferta`.
    """
    ofertas = list(OportunidadOferta.objects.filter(oportunidad_id=op.id))
    ahora = col_now()
    historial = []
    a_mover = []

    with transaction.atomic():
        for o in ofertas:
            actual = valor(o.estado)
            if actual == estado:
                continue
            historial.append(OportunidadEstadoHistorial(
                oportunidad_id=op.id, oferta_id=o.id, estado_anterior=actual,
                estado_nuevo=estado, usuario_id=getattr(usuario, "id", None),
            ))
            o.estado = estado
            o.estado_desde = ahora
            o.resultado = estado_a_resultado(estado)
            a_mover.append(o)
        movidas = len(a_mover)

        # Espejo en la columna deprecada: hay históricos y consultas que aún la leen.
        if valor(op.estado) != estado:
            if not ofertas:
                historial.append(OportunidadEstadoHistorial(
                    oportunidad_id=op.id, estado_anterior=valor(op.estado),
                    estado_nuevo=estado, usuario_id=getattr(usuario, "id", None),
                ))
            op.estado = estado
            op.estado_desde = ahora
            op.save(update_fields=["estado", "estado_desde"])
        elif not movidas:
            raise Conflict(f"Todo el negocio ya está en '{estado}'")

        if historial:
            OportunidadEstadoHistorial.objects.bulk_create(historial)
        if a_mover:
            OportunidadOferta.objects.bulk_update(
                a_mover, ["estado", "estado_desde", "resultado"]
            )

    return {"ok": True, "estado": estado, "estado_desde": ahora, "ofertas_movidas": movidas}


def mover_oferta(oferta: OportunidadOferta, estado: str, usuario) -> None:
    """Mueve UNA oferta de etapa: la operación normal del tablero.

    Una oferta se firma sin arrastrar a sus hermanas del mismo cliente.
    """
    actual = valor(oferta.estado)
    if estado == actual:
        raise Conflict(f"La oferta ya está en '{actual}'")
    with transaction.atomic():
        OportunidadEstadoHistorial.objects.create(
            oportunidad_id=oferta.oportunidad_id, oferta_id=oferta.id,
            estado_anterior=actual, estado_nuevo=estado,
            usuario_id=getattr(usuario, "id", None),
        )
        oferta.estado = estado
        oferta.estado_desde = col_now()
        oferta.resultado = estado_a_resultado(estado)
        oferta.save(update_fields=["estado", "estado_desde", "resultado"])


# ── Firma: la oferta evoluciona en su contrato PPA ───────────────────────────

def plantas_de_la_oferta(oferta) -> list[Proyecto]:
    """Las plantas asociadas: las de la M2M si tiene, y si no la del `proyecto_id`
    único. Mismo criterio que la vista PPA-céntrica, para que lo que se firma sea
    exactamente lo que se venía mostrando en el borrador."""
    ids = list(
        OportunidadOfertaProyecto.objects
        .filter(oferta_id=oferta.id)
        .values_list("proyecto_id", flat=True)
    )
    if not ids and oferta.proyecto_id:
        ids = [oferta.proyecto_id]
    if not ids:
        return []
    return list(Proyecto.objects.filter(id__in=ids, deleted_at__isnull=True))


def tarifa_del_primer_anio(datos: dict) -> float | None:
    """`tarifa_base` del contrato cuando el precio viene como tabla por año."""
    precios = datos.get("precios_anuales") or []
    if not precios:
        return None
    return min(precios, key=lambda p: p["anio"])["precio"]


def tarifas_mensuales(datos: dict) -> list[dict]:
    """Expande la tabla anual de la oferta a filas `(año, mes, tarifa)`.

    `ppa_tarifas` es mensual porque los contratos viejos indexan mes a mes; las
    ofertas nuevas traen un solo precio por año, así que se replica en sus 12
    meses. Se recorta al período de suministro: un contrato que arranca en
    octubre no tiene tarifa de enero a septiembre de ese año.
    """
    precios = datos.get("precios_anuales") or []
    if not precios:
        return []
    inicio, fin = datos["fecha_inicio"], datos["fecha_fin"]
    filas = []
    for p in precios:
        anio = p["anio"]
        if anio < inicio.year or anio > fin.year:
            continue          # año fuera del período: la oferta lo trae de más
        desde = inicio.month if anio == inicio.year else 1
        hasta = fin.month if anio == fin.year else 12
        filas += [
            {"año": anio, "mes": mes, "tarifa": p["precio"]}
            for mes in range(desde, hasta + 1)
        ]
    return filas


def firmar(oferta: OportunidadOferta, datos: dict, usuario) -> tuple[PpaContrato, int]:
    """Crea el PPA con las condiciones pactadas y lo enlaza a la oferta.

    Devuelve `(contrato, n_plantas)`.
    """
    if oferta.ppa_contrato_id:
        raise Conflict(f"La oferta ya tiene el contrato PPA {oferta.ppa_contrato_id}")
    if valor(oferta.tipo) not in TIPOS_ENERGIA:
        raise NoProcesable(
            "Solo las ofertas de energía (compra o comunidad energética) derivan "
            "en un PPA; las de servicios usan el contrato de representación"
        )
    op = get_oportunidad(oferta.oportunidad_id)
    cliente = Cliente.objects.filter(pk=op.cliente_id).first()
    plantas = plantas_de_la_oferta(oferta)
    filas_tarifa = tarifas_mensuales(datos)

    with transaction.atomic():
        contrato = PpaContrato.objects.create(
            numero_codigo_contrato=(
                datos.get("numero_codigo_contrato") or norm_codigo(oferta.numero_oferta)
            ),
            nombre_interno=datos.get("nombre_interno") or oferta.planta_nombre,
            # Unergy COMPRA la energía al generador: el cliente de la oferta vende.
            vendedor_id=op.cliente_id,
            vendedor_nombre=cliente.razon_social_nombre if cliente else None,
            vendedor_nit=cliente.nit_cedula if cliente else None,
            fecha_inicio=datos.get("fecha_inicio"),
            fecha_fin=datos.get("fecha_fin"),
            tarifa_base=datos.get("tarifa_base") or tarifa_del_primer_anio(datos),
            indice_indexacion=datos.get("indice_indexacion"),
            periodo_indexacion_base=datos.get("periodo_indexacion_base"),
            cantidad_minima_kwh_mes=datos.get("cantidad_minima_kwh_mes"),
            tipo_contrato="compra",
            # La característica pasa a vivir en el CONTRATO: si mañana se borra la
            # oferta, el PPA sigue sabiendo lo que es.
            es_comunidad_energetica=(valor(oferta.tipo) == "comunidad_energetica"),
        )
        # TODAS las plantas de la oferta, no solo la del `proyecto_id`: una oferta
        # que cubre dos plantas debe firmar un contrato con las dos, o Cumplimiento
        # mediría el compromiso entero contra la generación de media planta.
        PpaContratoProyecto.objects.bulk_create([
            PpaContratoProyecto(contrato_id=contrato.id, proyecto_id=p.id) for p in plantas
        ])
        if datos.get("carpeta_link"):
            set_enlace_documento(
                ppa_contrato_id=contrato.id, url=datos["carpeta_link"],
                nombre="Enlace Drive del contrato",
            )
        PpaTarifa.objects.bulk_create([
            PpaTarifa(contrato_id=contrato.id, **fila) for fila in filas_tarifa
        ])

        oferta.ppa_contrato_id = contrato.id
        campos = ["ppa_contrato_id"]
        anterior = valor(oferta.estado)
        if anterior != "firmado":
            OportunidadEstadoHistorial.objects.create(
                oportunidad_id=op.id, oferta_id=oferta.id, estado_anterior=anterior,
                estado_nuevo="firmado", usuario_id=getattr(usuario, "id", None),
            )
            oferta.estado = "firmado"
            oferta.estado_desde = col_now()
            oferta.resultado = estado_a_resultado("firmado")
            campos += ["estado", "estado_desde", "resultado"]
        oferta.save(update_fields=campos)

    return contrato, len(plantas)
