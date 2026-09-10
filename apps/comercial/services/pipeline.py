"""El pipeline comercial: etapas, alertas y la ficha operativa de cada planta.

Puerto de `app/services/comercial.py`. De sus 1 518 líneas, unas 900 son
aritmética de etapas y armado de fichas —se movieron verbatim— y ~430
consultaban la base.

**`proponer_vinculos_proyecto` NO escribe, y eso es la mitad del diseño.** El
pipeline se cargó desde hojas de cálculo, donde la planta es texto libre; el
matcher propone y una persona decide, porque un match equivocado le pone a una
planta la generación y el contrato de otra — un error silencioso.

**`_resolver_ppas` tiene dos caminos y el implícito no es un lujo:** en
producción NINGÚN PPA está enlazado a su oferta, porque los contratos son
anteriores al CRM. Sin el camino por planta, toda planta con contrato saldría
como `sin_contrato`.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import ExtractMonth, ExtractYear

from apps.comun.nombre_matching import mejor_candidato
from apps.comun.series_mensuales import serie_mensual_kwh
from apps.plataforma.services.fechas import hoy_col

ETAPAS = ("oportunidad", "oferta", "contrato", "firmado", "operando", "terminado")
ETAPA_ORDEN = {e: i for i, e in enumerate(ETAPAS)}

# Etapas donde aplica la alerta de días sin respuesta. Las de cierre y la
# negativa quedan por fuera a propósito: no requieren seguimiento comercial.
ESTADOS_CON_ALERTA = frozenset({"oportunidad", "oferta", "contrato"})

# `resultado` (pendiente/aceptado/declinado) dejó de ser un campo editable el
# 2026-08-02: se deriva de la etapa. Existían dos verdades para lo mismo y podían
# contradecirse (una oferta 'aceptada' colgando de un cliente 'declinado').
_RESULTADO_POR_ETAPA = {
    "oportunidad": "pendiente",
    "oferta": "pendiente",
    "contrato": "pendiente",
    "firmado": "aceptado",
    "operando": "aceptado",
    # Terminado sigue siendo un negocio ganado: corrió y se venció.
    "terminado": "aceptado",
    "declinado": "declinado",
}


def estado_a_resultado(estado: str) -> str:
    """Resultado que corresponde a una etapa del pipeline."""
    return _RESULTADO_POR_ETAPA.get(estado, "pendiente")


def cerrar_contratos_vencidos(hoy=None) -> list[dict]:
    """Pasa a 'terminado' las ofertas cuyo contrato ya llegó a su fecha_fin.

    Se hace por job y no a mano porque nadie va a entrar al CRM el 1-ene-2033 a
    cerrar Pelletco. La fecha_fin del PPA es el dato duro; la etapa lo sigue.
    Solo toca ofertas en 'operando' con contrato vencido: una firmada que aún no
    arranca, o una sin contrato, se quedan donde están.

    Devuelve las transiciones aplicadas (vacío si no hubo).
    """
    from apps.comercial.models import OportunidadEstadoHistorial, OportunidadOferta

    hoy = hoy or hoy_col()
    filas = list(
        OportunidadOferta.objects
        .filter(
            estado="operando",
            ppa_contrato__fecha_fin__isnull=False,
            ppa_contrato__fecha_fin__lt=hoy,
            ppa_contrato__deleted_at__isnull=True,
        )
        .select_related("ppa_contrato")
    )
    if not filas:
        return []

    ahora = col_now()
    cerradas = []
    with transaction.atomic():
        OportunidadEstadoHistorial.objects.bulk_create([
            OportunidadEstadoHistorial(
                oportunidad_id=o.oportunidad_id, oferta_id=o.id,
                estado_anterior="operando", estado_nuevo="terminado",
            )
            for o in filas
        ])
        for oferta in filas:
            oferta.estado = "terminado"
            oferta.estado_desde = ahora
            oferta.resultado = estado_a_resultado("terminado")
            cerradas.append({
                "oferta_id": oferta.id, "codigo": oferta.numero_oferta,
                "fecha_fin": str(oferta.ppa_contrato.fecha_fin),
            })
        OportunidadOferta.objects.bulk_update(
            filas, ["estado", "estado_desde", "resultado"]
        )
    return cerradas


def resumen_etapas(estados) -> dict:
    """Cuántas ofertas del cliente hay en cada etapa, p.ej. {'firmado': 1, 'oferta': 2}.

    Un CLIENTE no tiene etapa: el negocio es la oferta (Juan, 2026-08-02). Lo que
    diferencia a las ofertas de un mismo cliente son las plantas. Por eso su
    ficha resume las etapas de sus ofertas en vez de inventar una sola —
    colapsarlas escondería justo lo que hay que ver: Tecni-plast tiene una
    firmada y otra abierta, y ninguna de las dos define "el estado del cliente".
    """
    out: dict = {}
    for e in estados:
        out[e] = out.get(e, 0) + 1
    return out


def col_now() -> datetime:
    """Ahora en hora Colombia (UTC-5, sin DST) — patrón del repo.

    OJO: la hora es la correcta pero el tzinfo queda en UTC, así que serializado
    dice "+00:00" sobre una hora que es -05:00. Para uso interno da igual (todas
    las cuentas se hacen entre valores de esta misma función), pero **no lo
    devuelvas en una respuesta de API**: quien la parsee se corre cinco horas.
    Para eso está `ahora_colombia()`.
    """
    return datetime.now(timezone.utc) - timedelta(hours=5)


COLOMBIA = timezone(timedelta(hours=-5))


def ahora_colombia() -> datetime:
    """Ahora en Colombia, con el offset bien puesto (-05:00).

    La versión honesta de col_now(), para lo que sale hacia afuera.
    """
    return datetime.now(COLOMBIA)


def calcular_alerta(
    estado: str,
    estado_desde: datetime,
    ultima_gestion: datetime | None,
    umbral_dias: int,
    ahora: datetime,
) -> tuple[int, bool]:
    """Devuelve (dias_sin_respuesta, alerta).

    La referencia es lo MÁS RECIENTE entre la entrada al estado actual y la
    última gestión de bitácora. Alerta solo con MÁS de `umbral_dias` días
    (5 días exactos NO alertan) y solo en ESTADOS_CON_ALERTA.
    """
    # Defensivo: si algún datetime viene naive (p.ej. roundtrip por un backend
    # sin timezone), se alinea al tz de `ahora` para no romper la resta.
    def _com(dt):
        if dt is not None and dt.tzinfo is None and ahora.tzinfo is not None:
            return dt.replace(tzinfo=ahora.tzinfo)
        return dt

    estado_desde = _com(estado_desde)
    ultima_gestion = _com(ultima_gestion)
    referencia = estado_desde
    if ultima_gestion is not None and ultima_gestion > referencia:
        referencia = ultima_gestion
    dias = (ahora - referencia).days
    if dias < 0:
        dias = 0
    alerta = estado in ESTADOS_CON_ALERTA and dias > umbral_dias
    return dias, alerta


def meses_de_contrato(inicio, fin) -> int | None:
    """Meses calendario que cubre el suministro, contando el primero y el último.

    Se cuenta por mes y no por días porque el PPA se factura por mes: es
    exactamente el número de filas de `ppa_tarifas` que genera /firmar al
    expandir el periodo. 12-feb-2026 → 31-dic-2032 son 83 meses de suministro,
    no "6 años y pico".
    """
    if inicio is None or fin is None or fin < inicio:
        return None
    return (fin.year - inicio.year) * 12 + (fin.month - inicio.month) + 1


def ficha_operativa(oferta, proyecto=None, ppa=None, generacion=None,
                    operador_oferta=None) -> dict:
    """Los 6 parámetros que el equipo consume por API, resueltos por cascada.

    La cascada es POR CAMPO: lo que diga el Proyecto manda, si no hay Proyecto o
    el dato está vacío vale lo declarado en la oferta, si no null. Por campo y no
    por entidad porque un Proyecto a medio diligenciar no debe borrar lo que la
    oferta sí sabe.

    Devuelve valores PLANOS más un mapa `fuentes` aparte, en vez de envolver cada
    campo en {valor, fuente}: quien consume la API lee `ficha.municipio` directo,
    y quien necesita auditar de dónde salió el dato mira `fuentes`. Sin ese mapa,
    un null y un "todavía no lo sabemos" se ven igual.

    No toca la BD. `generacion` —("2026-07", kwh) del último mes cerrado— y
    `operador_oferta` —nombre legal del catálogo para oferta.operador_red_id—
    los precarga contexto_ficha() por lotes.
    """
    fuentes: dict[str, str | None] = {}

    def _elegir(campo, del_proyecto, de_la_oferta):
        if del_proyecto not in (None, ""):
            fuentes[campo] = "proyecto"
            return del_proyecto
        if de_la_oferta not in (None, ""):
            fuentes[campo] = "oferta"
            return de_la_oferta
        fuentes[campo] = None
        return None

    proyecto_nombre = _elegir(
        "proyecto_nombre",
        proyecto.nombre_comercial if proyecto else None,
        oferta.planta_nombre)
    municipio = _elegir("municipio",
                        proyecto.municipio if proyecto else None,
                        oferta.municipio)
    departamento = _elegir("departamento",
                           proyecto.departamento if proyecto else None,
                           oferta.departamento)

    # Operador de red: la cascada operador propio → primera frontera que lo tenga
    # ya vive en Proyecto.operador_red_legal; aquí solo se le agrega el escalón
    # de lo declarado en la oferta.
    operador_red = _elegir("operador_red",
                           operador_red_legal(proyecto) if proyecto else None,
                           operador_oferta)
    if fuentes["operador_red"] == "proyecto":
        # Puede quedar None si el nombre salió de una frontera y no del proyecto:
        # el nombre es el dato, el id es la conveniencia.
        operador_red_id = proyecto.operador_red_id
    elif fuentes["operador_red"] == "oferta":
        operador_red_id = oferta.operador_red_id
    else:
        operador_red_id = None

    # Energía promedio = generación mensual ESTIMADA (decisión de Juan). El
    # proyecto habla en MWh/mes; el CRM en kWh/mes, como cantidad_minima_kwh_mes.
    promedio_proyecto = None
    if proyecto is not None:
        # Ojo: el p50 llega como lista o como texto JSON según la antigüedad
        # de la fila (ver serie_mensual_kwh). Leerlo crudo tumbaba la lista
        # entera de /comercial/ofertas con un 500.
        vals = serie_mensual_kwh(proyecto.p50_mensual_kwh)
        if vals:
            promedio_proyecto = sum(vals) / len(vals)
    energia_promedio = _elegir(
        "energia_promedio_kwh_mes", promedio_proyecto,
        float(oferta.energia_promedio_kwh_mes)
        if oferta.energia_promedio_kwh_mes is not None else None)

    # Energía real: la del último mes CERRADO, con su periodo al lado para que
    # nadie compare contra un mes a medias.
    energia_real, energia_real_periodo = None, None
    if generacion:
        energia_real_periodo, kwh = generacion
        energia_real = float(kwh) if kwh is not None else None
    fuentes["energia_real_kwh_mes"] = "generacion" if energia_real is not None else None

    # Fecha de inicio de operación = inicio de suministro del PPA (decisión de
    # Juan). Sin contrato firmado sirve la tentativa, pero marcada como estimada.
    contrato_inicio = ppa.fecha_inicio if ppa else None
    contrato_fin = ppa.fecha_fin if ppa else None
    if contrato_inicio is not None:
        fecha_inicio_operacion = contrato_inicio
        fuentes["fecha_inicio_operacion"] = "contrato"
    elif oferta.fecha_tentativa_inicio is not None:
        fecha_inicio_operacion = oferta.fecha_tentativa_inicio
        fuentes["fecha_inicio_operacion"] = "estimada"
    else:
        fecha_inicio_operacion = None
        fuentes["fecha_inicio_operacion"] = None

    meses = meses_de_contrato(contrato_inicio, contrato_fin)
    anios = None
    if meses is not None:
        anios = float((Decimal(meses) / Decimal(12)).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP))
    fuentes["contrato_compra_meses"] = "contrato" if meses is not None else None

    return {
        "proyecto_nombre": proyecto_nombre,
        "municipio": municipio,
        "departamento": departamento,
        "operador_red": operador_red,
        "operador_red_id": operador_red_id,
        "energia_promedio_kwh_mes": energia_promedio,
        "energia_real_kwh_mes": energia_real,
        "energia_real_periodo": energia_real_periodo,
        "fecha_inicio_operacion": fecha_inicio_operacion,
        "contrato_compra_meses": meses,
        "contrato_compra_anios": anios,
        "contrato_fecha_inicio": contrato_inicio,
        "contrato_fecha_fin": contrato_fin,
        "fuentes": fuentes,
    }


# ── Plantas firmadas y operando (consumo externo) ─────────────────────────────
# Superficie de solo lectura para integrar la plataforma con otra: "dame las
# plantas con negocio cerrado y sus datos". Vive acá y no en proyectos.py porque
# en qué etapa está cada planta lo define el pipeline comercial
# (`oportunidad_ofertas.estado`), que es lo que se ve en /comercial.
#
# La diferencia con ficha_operativa(): esta habla de PROYECTOS (una fila por
# planta, aunque tenga dos ofertas), usa la generación promedio MEDIDA
# (`proyectos.gen_mensual_promedio_mwh`) en vez de la estimada, y trae la fecha
# de inicio de COMERCIALIZACIÓN, que no es la de inicio del contrato.

ESTADO_OPERANDO = "operando"
ESTADO_FIRMADO = "firmado"

# Las dos etapas de negocio cerrado, que son las que se entregan hacia afuera:
# `firmado` = hay contrato pero el suministro todavía no arrancó; `operando` = ya
# está entregando energía. Antes solo se devolvía `operando`; se agregó `firmado`
# porque son plantas comprometidas y quien integra necesita verlas, con la etapa
# al lado para poder distinguirlas.
ETAPAS_ENTREGABLES = (ESTADO_FIRMADO, ESTADO_OPERANDO)

# Todo el pipeline consultable desde afuera. `terminado` y `declinado` son
# SALIDAS, no avances: la primera es un negocio que corrió y se venció, la
# segunda uno que nunca fue.
ETAPAS_ACTIVAS = ("oportunidad", "oferta", "contrato", ESTADO_FIRMADO, ESTADO_OPERANDO)
ETAPAS_SALIDA = ("terminado", "declinado")
ETAPAS_CONSULTABLES = ETAPAS_ACTIVAS + ETAPAS_SALIDA

# Cómo se elige la etapa de una PLANTA que tiene varias ofertas: cualquier etapa
# activa le gana a cualquier salida, y entre activas gana la más avanzada.
#
# No alcanza con ETAPA_ORDEN, donde `terminado` es el último y por lo tanto el
# "más avanzado": una planta que ya opera y que además tiene un contrato viejo
# terminado saldría como `terminado` y desaparecería de la consulta por defecto,
# cuando la verdad es que está entregando energía. Entre dos salidas gana
# `terminado`, que al menos llegó a operar.
_RANGO_ETAPA = {e: (1, i) for i, e in enumerate(ETAPAS_ACTIVAS)}
_RANGO_ETAPA.update({"terminado": (0, 1), "declinado": (0, 0)})


def etapa_de_la_planta(estados) -> str:
    """La etapa que representa a una planta a partir de las de sus ofertas."""
    return max(estados, key=lambda e: _RANGO_ETAPA.get(e, (-1, 0)))

# De dónde salió la generación promedio. Se nombra por su naturaleza y no por la
# columna: quien integra necesita saber si el número está medido o estimado, no
# en qué tabla vive.
GEN_MEDIDO = "medido"        # ventana móvil de 30 días de generación real
GEN_MANUAL = "manual"        # lo cargó una persona (planta sin histórico)
GEN_ESTIMADO = "estimado"    # proyección del proyecto (promedio de la curva p50)
GEN_DECLARADO = "declarado"  # lo declaró la oferta comercial (planta sin Proyecto)


def duracion_contrato(inicio, fin, hoy=None) -> dict:
    """Cuánto dura el contrato de energía y cuánto le queda.

    `meses` son meses calendario (ver meses_de_contrato) porque el PPA se factura
    por mes. `texto` es la forma en que lo dice la gente ("6 años y 11 meses") y
    existe para que quien integre no tenga que rearmarla en su front.

    `meses_restantes` se cuenta desde hoy: un contrato que vence este mes tiene 1
    mes restante y uno vencido tiene 0. Si **todavía no arrancó** (caso de las
    plantas en etapa `firmado`) le queda su duración completa, no la distancia
    hasta el fin — contar desde hoy daría más meses restantes que meses de
    contrato, que es imposible.
    """
    meses = meses_de_contrato(inicio, fin)
    anios = None
    texto = None
    if meses is not None:
        anios = float((Decimal(meses) / Decimal(12)).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP))
        a, m = divmod(meses, 12)
        partes = []
        if a:
            partes.append(f"{a} año" if a == 1 else f"{a} años")
        if m or not a:
            partes.append(f"{m} mes" if m == 1 else f"{m} meses")
        texto = " y ".join(partes)

    hoy = hoy or col_now().date()
    restantes = None
    if fin is not None:
        if inicio is not None and inicio > hoy:
            restantes = meses          # firmado, sin arrancar
        else:
            restantes = meses_de_contrato(hoy, fin) or 0
    vigente = None
    if inicio is not None or fin is not None:
        vigente = ((inicio is None or inicio <= hoy)
                   and (fin is None or fin >= hoy))

    return {
        "fecha_inicio": inicio,
        "fecha_fin": fin,
        "duracion_meses": meses,
        "duracion_anios": anios,
        "duracion_texto": texto,
        "meses_restantes": restantes,
        "vigente": vigente,
    }


def operador_red_legal(proyecto) -> str | None:
    """Nombre legal del operador de red: primero el del catálogo del proyecto y,
    si no lo tiene, el de alguna de sus fronteras VIVAS.

    Una frontera borrada no debe seguir prestando su operador. Requiere el
    operador y las fronteras precargados — ver `_opciones_proyecto`.
    """
    if proyecto.operador_red_id and proyecto.operador_red:
        return proyecto.operador_red.nombre_legal
    for f in proyecto.fronteras.all():
        if f.deleted_at is None and f.operador_red_id and f.operador_red:
            return f.operador_red.nombre_legal
    return None


def _gen_promedio(proyecto, ofertas) -> tuple[float | None, str | None, dict]:
    """Generación mensual promedio en MWh, su origen y el detalle de la medición.

    La cascada va de lo más duro a lo más blando y el origen viaja siempre al
    lado del número: un promedio medido sobre 30 días de lecturas y una
    proyección de ingeniería no son el mismo dato aunque ocupen la misma casilla.

        medido/manual (proyectos.gen_mensual_promedio_mwh)
          → estimado (promedio de la curva p50)
          → declarado en la oferta (planta que todavía no existe como Proyecto)
    """
    detalle: dict = {"dias_con_datos": None, "ventana_desde": None,
                     "ventana_hasta": None, "actualizado_en": None}

    if proyecto is not None and proyecto.gen_mensual_promedio_mwh is not None:
        origen = (GEN_MANUAL if proyecto.gen_promedio_origen == "manual"
                  else GEN_MEDIDO)
        detalle = {
            "dias_con_datos": proyecto.gen_promedio_dias,
            "ventana_desde": proyecto.gen_promedio_desde,
            "ventana_hasta": proyecto.gen_promedio_hasta,
            "actualizado_en": proyecto.gen_promedio_actualizado_en,
        }
        return float(proyecto.gen_mensual_promedio_mwh), origen, detalle

    if proyecto is not None:
        # El p50 llega como lista o como texto JSON según la antigüedad de la
        # fila; serie_mensual_kwh() absorbe las dos formas.
        vals = serie_mensual_kwh(proyecto.p50_mensual_kwh)
        if vals:
            return round(sum(vals) / len(vals) / 1000.0, 3), GEN_ESTIMADO, detalle

    for o in ofertas:
        if o.energia_promedio_kwh_mes is not None:
            return (round(float(o.energia_promedio_kwh_mes) / 1000.0, 3),
                    GEN_DECLARADO, detalle)

    return None, None, detalle
def _oferta_min(o) -> dict:
    """La oferta como se ve desde afuera. Misma forma en `oferta_vigente` y en
    `ofertas[]` a propósito: quien integra escribe un solo lector."""
    return {
        "oferta_id": o.id,
        "codigo_seguimiento": _codigo_seguimiento(o.numero_oferta),
        "tipo": _valor_enum(o.tipo),
        "estado": _valor_enum(o.estado),
        "oportunidad_id": o.oportunidad_id,
    }


def _codigo_seguimiento(numero: str | None) -> str | None:
    """Prefijo estandarizado OF→OP del código de seguimiento. Idempotente.

    Duplica a propósito `app/api/v1/comercial.py::_norm_codigo`: importarlo desde
    la API metería la capa de rutas dentro de la de servicios.
    """
    if numero and numero[:2].upper() == "OF":
        return "OP" + numero[2:]
    return numero
def _valor_enum(v):
    """Enum de SQLAlchemy → slug; deja pasar lo que ya es str puro o None.

    El `isinstance(v, str)` se chequea DESPUÉS del enum, y no antes, porque los
    enums del CRM heredan de `str` (`class EstadoComercialEnum(str, Enum)`): con
    el orden inverso esta función devolvía el miembro del enum tal cual y no
    normalizaba nada, pese al nombre. Las comparaciones seguían andando —un
    str-enum es igual a su valor— y FastAPI lo serializaba bien, así que no se
    notaba; pero cualquier lector en Python recibía `EstadoComercialEnum.firmado`
    y al imprimirlo o usarlo de clave veía eso mismo en vez de `"firmado"`.
    """
    if v is None:
        return None
    return v.value if isinstance(v, enum.Enum) else v


# Umbral para proponer un vínculo oferta→proyecto. Bastante más alto que el
# UMBRAL_ACEPTAR=0.55 del matcher: con 0.55 la dedup de clientes llegó a fusionar
# falsos positivos (Soluenergías→FEM, 0.59). Acá el costo de un match malo es que
# una planta muestre la generación y el contrato de OTRA, así que se prefiere no
# proponer nada y que quede en la lista de revisión manual.
UMBRAL_VINCULO = 0.72


def proponer_vinculos_proyecto(estados=ETAPAS_ENTREGABLES,
                               umbral=UMBRAL_VINCULO) -> dict:
    """Empareja por nombre las ofertas sin `proyecto_id` con proyectos existentes.

    Existe porque el pipeline comercial se cargó desde hojas de cálculo, donde la
    planta es texto libre: "Catedral" contra "La Catedral", "Taurus IX" contra
    "GD Taurus IX". Son la misma planta y sin el vínculo la API de plantas
    operando devuelve el nombre y nada más.

    **NO escribe: solo propone.** La decisión de vincular es de una persona,
    porque un match equivocado le pone a una planta la generación y el contrato
    de otra — un error silencioso, que es el peor tipo. Quien la aplique es
    `vincular_proyectos()`.

    Usa el matcher compartido (`mejor_candidato`), que tiene guarda de
    ambigüedad: si dos proyectos quedan parejos, no adivina.
    """
    from apps.comercial.models import OportunidadOferta
    from apps.proyectos.models import Proyecto

    qs = OportunidadOferta.objects.filter(
        proyecto_id__isnull=True, oportunidad__deleted_at__isnull=True,
    )
    if estados:
        qs = qs.filter(estado__in=tuple(estados))
    ofertas = list(qs)

    candidatos = [
        (p, [p.nombre_comercial] if p.nombre_comercial else [])
        for p in Proyecto.objects.filter(deleted_at__isnull=True)
    ]

    propuestos, sin_candidato, sin_nombre = [], [], []
    for o in ofertas:
        if not (o.planta_nombre or "").strip():
            sin_nombre.append({
                "oferta_id": o.id, "codigo": _codigo_seguimiento(o.numero_oferta),
            })
            continue
        proyecto, score = mejor_candidato(o.planta_nombre, candidatos)
        fila = {
            "oferta_id": o.id,
            "codigo": _codigo_seguimiento(o.numero_oferta),
            "planta_nombre": o.planta_nombre,
            "estado": _valor_enum(o.estado),
            "score": score,
        }
        if proyecto is not None and score >= umbral:
            fila.update({
                "proyecto_id": proyecto.id, "proyecto_nombre": proyecto.nombre_comercial,
            })
            propuestos.append(fila)
        else:
            # Se reporta el mejor score aunque no alcance: dice si faltó poco
            # (revisar a mano) o si no hay nada parecido (crear la planta).
            sin_candidato.append(fila)

    return {
        "umbral": umbral,
        "estados": list(estados) if estados else "todas",
        "n_ofertas_sin_proyecto": len(ofertas),
        "n_propuestos": len(propuestos),
        "n_sin_candidato": len(sin_candidato),
        "n_sin_nombre": len(sin_nombre),
        # Las tres listas son el valor del reporte: qué se va a vincular, qué hay
        # que mirar a mano y qué ni siquiera tiene nombre de planta.
        "propuestos": sorted(propuestos, key=lambda f: -f["score"]),
        "sin_candidato": sorted(sin_candidato, key=lambda f: -f["score"]),
        "sin_nombre": sin_nombre,
    }


def vincular_proyectos(estados=ETAPAS_ENTREGABLES, umbral=UMBRAL_VINCULO,
                       dry_run=True, solo_ofertas=None) -> dict:
    """Aplica los vínculos que propone `proponer_vinculos_proyecto`.

    Idempotente: solo toca ofertas con `proyecto_id` en NULL, así que repetirla
    no cambia nada. `solo_ofertas` limita la escritura a una lista de ids — el
    camino para aceptar unas propuestas y descartar otras sin subir el umbral.

    Reversible a mano: deshacer un vínculo es poner `proyecto_id` en NULL desde
    la ficha de la oferta.
    """
    from apps.comercial.models import OportunidadOferta

    reporte = proponer_vinculos_proyecto(estados=estados, umbral=umbral)
    a_aplicar = reporte["propuestos"]
    if solo_ofertas is not None:
        permitidas = set(solo_ofertas)
        reporte["omitidos_por_filtro"] = [
            f for f in a_aplicar if f["oferta_id"] not in permitidas
        ]
        a_aplicar = [f for f in a_aplicar if f["oferta_id"] in permitidas]

    if not dry_run and a_aplicar:
        por_id = {
            o.id: o for o in OportunidadOferta.objects.filter(
                id__in=[f["oferta_id"] for f in a_aplicar]
            )
        }
        a_guardar = []
        for fila in a_aplicar:
            oferta = por_id.get(fila["oferta_id"])
            if oferta is not None and oferta.proyecto_id is None:
                oferta.proyecto_id = fila["proyecto_id"]
                a_guardar.append(oferta)
        if a_guardar:
            OportunidadOferta.objects.bulk_update(a_guardar, ["proyecto_id"])

    reporte.update({
        "dry_run": dry_run,
        "n_aplicados": 0 if dry_run else len(a_aplicar),
        "aplicados": [] if dry_run else a_aplicar,
    })
    return reporte


def _mejor_ppa(contratos, hoy):
    """El contrato de energía que describe a la planta HOY.

    Prioridad: vigente hoy → de compra (que es el que firma el CRM: Unergy le
    compra la energía al dueño de la planta) → el de inicio más reciente. Un
    contrato vencido de 2021 no puede ganarle al que está corriendo.
    """
    if not contratos:
        return None

    def _clave(c):
        vigente = ((c.fecha_inicio is None or c.fecha_inicio <= hoy)
                   and (c.fecha_fin is None or c.fecha_fin >= hoy))
        return (0 if vigente else 1,
                0 if (c.tipo_contrato or "venta") == "compra" else 1,
                -(c.fecha_inicio.toordinal() if c.fecha_inicio else 0))

    return sorted(contratos, key=_clave)[0]


def contexto_ficha(ofertas, hoy=None) -> dict[int, dict]:
    """Precarga lo que `ficha_operativa()` necesita: `{oferta_id: kwargs}`.

    Un número FIJO de consultas sin importar cuántas ofertas entren. Resolverlo
    dentro del bucle sería N+1 justo en la vista principal de /comercial, que
    carga todas las ofertas de una.

    Las llaves de cada valor son los nombres de los parámetros de
    `ficha_operativa`, para poder llamarla como `ficha_operativa(o, **ctx[o.id])`.
    """
    from apps.fronteras.models import OperadorRed
    from apps.ppa.models import PpaContrato

    ofertas = list(ofertas)
    if not ofertas:
        return {}

    proyecto_ids = {o.proyecto_id for o in ofertas if o.proyecto_id}
    ppa_ids = {o.ppa_contrato_id for o in ofertas if o.ppa_contrato_id}
    operador_ids = {o.operador_red_id for o in ofertas if o.operador_red_id}

    proyectos = {}
    if proyecto_ids:
        # `operador_red_legal` recorre el operador y las fronteras: sin precargar
        # serían dos consultas por proyecto.
        proyectos = {
            p.id: p for p in _proyectos_con_ficha(Q(id__in=proyecto_ids))
        }
    ppas = {}
    if ppa_ids:
        # Un contrato borrado no alimenta la ficha: la oferta conserva el FK pero
        # sus fechas ya no son la verdad de nadie.
        ppas = {
            c.id: c for c in PpaContrato.objects.filter(
                id__in=ppa_ids, deleted_at__isnull=True,
            )
        }
    operadores = {}
    if operador_ids:
        operadores = dict(
            OperadorRed.objects.filter(id__in=operador_ids)
            .values_list("id", "nombre_legal")
        )

    generacion = _ultimo_mes_generacion(proyecto_ids, hoy=hoy)

    return {
        o.id: {
            "proyecto": proyectos.get(o.proyecto_id),
            "ppa": ppas.get(o.ppa_contrato_id),
            "generacion": generacion.get(o.proyecto_id),
            "operador_oferta": operadores.get(o.operador_red_id),
        }
        for o in ofertas
    }


def _ultimo_mes_generacion(proyecto_ids, hoy=None) -> dict[int, tuple[str, float]]:
    """Último mes CERRADO con lecturas por proyecto: `{proyecto_id: ('2026-07', kwh)}`.

    Dos exclusiones a propósito, las dos por lo mismo — un número parcial
    presentado como energía real del mes es peor que no dar número:

      · el mes en curso no cuenta (tres días de agosto no son un mes);
      · un mes con menos de 28 días de lectura tampoco.

    Una sola consulta agregada para todos los proyectos.
    """
    from apps.proyectos.models import GeneracionDiaria

    if not proyecto_ids:
        return {}
    hoy = hoy or col_now().date()
    filas = (
        GeneracionDiaria.objects
        .filter(
            proyecto_id__in=proyecto_ids,
            kwh_real__isnull=False,
            fecha__lt=hoy.replace(day=1),
        )
        .values("proyecto_id")
        .annotate(anio=ExtractYear("fecha"), mes=ExtractMonth("fecha"))
        .values("proyecto_id", "anio", "mes")
        .annotate(kwh=Sum("kwh_real"), dias=Count("id"))
        .filter(dias__gte=28)
    )
    salida: dict[int, tuple[str, float]] = {}
    for f in filas:
        periodo = f"{int(f['anio']):04d}-{int(f['mes']):02d}"
        pid = f["proyecto_id"]
        if pid not in salida or periodo > salida[pid][0]:
            salida[pid] = (periodo, float(f["kwh"]))
    return salida


# ── La vista PPA-céntrica del pipeline ───────────────────────────────────────
#
# proyectos_operando() responde "¿qué plantas hay?"; esto responde "¿qué
# contratos de energía hay?" y cuelga las plantas de cada uno. Es el árbol
# PPA → PROYECTOS → detalles.
#
# La idea central: un PPA no firmado NO tiene fila en `ppa_contratos`. La oferta
# del CRM ES el PPA hasta que se firma, y `ppa["id"] is None` es la única señal
# de que todavía es un borrador. No hay un segundo estado que pueda contradecir
# al primero, y no hay filas de mentira ensuciando los 18 módulos que leen
# ppa_contratos (Cumplimiento, GESCON, liquidaciones, facturación).

# Solo los contratos de ENERGIA derivan en un PPA. Los servicios
# (representación, CGM) desembocan en contratos_servicio, que es otra entidad.
# `comunidad_energetica` NO es un tipo de contrato aparte: es un PPA con una
# característica, y por eso entra acá y se marca en el nodo.
TIPOS_ENERGIA = ("compra_energia", "comunidad_energetica")

# Las etapas que producen un PPA (borrador o materializado). `oportunidad` queda
# afuera porque todavía no hay oferta de la que derivar condiciones, y las
# salidas (declinado/terminado) porque un negocio caído no es un contrato en
# preparación: sería basura con forma de PPA.
ETAPAS_CON_PPA = ("oferta", "contrato", ESTADO_FIRMADO, ESTADO_OPERANDO)


def ppas_del_pipeline(q=None, hoy=None, estados=ETAPAS_CON_PPA) -> list[dict]:
    """Los contratos de energía del pipeline, con sus plantas colgando.

    Un nodo por PPA: `{"ppa": {...}, "proyectos": [{...con detalles}]}`.
    """
    from apps.comercial.models import OportunidadOferta

    hoy = hoy or col_now().date()
    estados = tuple(estados)

    ofertas = list(
        OportunidadOferta.objects.filter(
            tipo__in=TIPOS_ENERGIA,
            estado__in=estados,
            oportunidad__deleted_at__isnull=True,
        )
    )
    if not ofertas:
        return []

    clientes = _clientes_por_oportunidad({o.oportunidad_id for o in ofertas})
    operadores = _operadores_por_id(
        {o.operador_red_id for o in ofertas if o.operador_red_id})
    de_oferta = _proyectos_de_ofertas(ofertas)
    resuelto = _resolver_ppas(ofertas, de_oferta, hoy)
    del_ppa = _proyectos_por_ppa({p.id for p, _ in resuelto.values() if p is not None})

    # Un nodo por CONTRATO: si dos ofertas desembocan en el mismo PPA, el PPA
    # aparecería dos veces y `total` lo contaría doble. Sin contrato todavía no hay
    # nada por lo que agrupar, así que cada oferta es su propio borrador.
    grupos: dict[tuple, list] = {}
    for o in ofertas:
        ppa, _fuente = resuelto[o.id]
        clave = ("ppa", ppa.id) if ppa is not None else ("oferta", o.id)
        grupos.setdefault(clave, []).append(o)

    nodos = []
    for grupo in grupos.values():
        # La oferta que representa al grupo: manda la de compra de energía, que es
        # la que define el negocio; el id desempata.
        grupo = sorted(grupo, key=lambda o: (
            0 if _valor_enum(o.tipo) == "compra_energia" else 1, o.id))
        principal = grupo[0]
        ppa, fuente = resuelto[principal.id]
        # Para un PPA que existe las plantas son las del CONTRATO: esa es la verdad
        # contractual, y puede cubrir más plantas que una sola oferta. Para un
        # borrador todavía no hay contrato, así que son las que declaró la oferta.
        proyectos = (del_ppa.get(ppa.id, []) if ppa is not None
                     else de_oferta.get(principal.id, []))
        # Qué ofertas del grupo nombraron cada planta. Para un borrador son todas
        # sus plantas; para un contrato, las plantas del PPA que además alguna
        # oferta declaró (las demás quedan sin escalón de oferta, que es lo
        # correcto: nadie declaró nada sobre ellas). En el orden del grupo, así
        # que la de compra de energía manda.
        declarantes: dict[int, list] = {}
        for o in grupo:
            for p in de_oferta.get(o.id, []):
                declarantes.setdefault(p.id, []).append(o)
        nodos.append(_nodo_ppa(principal, ppa=ppa, fuente_ppa=fuente,
                               proyectos=proyectos, ofertas=grupo,
                               cliente=clientes.get(principal.oportunidad_id), hoy=hoy,
                               declarantes=declarantes, operadores=operadores))

    if q:
        aguja = q.strip().lower()
        nodos = [n for n in nodos if _coincide(n, aguja)]

    # Por nombre de planta para que la lista sea estable entre llamadas; el id
    # desempata cuando dos ofertas nombran la misma planta.
    nodos.sort(key=lambda n: ((n["ppa"]["planta_declarada"] or "").lower(),
                              n["ppa"]["oferta_id"]))
    return nodos


def _coincide(nodo, aguja: str) -> bool:
    """Busca en la planta declarada, el cliente, el código de seguimiento y el
    nombre de cada proyecto del PPA."""
    ppa = nodo["ppa"]
    campos = [ppa["planta_declarada"], ppa["cliente"], ppa["codigo_seguimiento"],
              ppa["numero_codigo_contrato"], ppa["nombre_interno"]]
    campos += [p["nombre"] for p in nodo["proyectos"]]
    return any(aguja in (c or "").lower() for c in campos)


def _proyectos_de_ofertas(ofertas) -> dict[int, list]:
    """Las plantas declaradas por cada oferta: las de la M2M si tiene, y si no la
    del `proyecto_id` único. Así las ofertas ya vinculadas siguen resolviendo su
    planta sin necesidad de backfill."""
    from apps.comercial.models import OportunidadOfertaProyecto

    ids = [o.id for o in ofertas]
    asociadas: dict[int, list[int]] = {}
    for oferta_id, proyecto_id in OportunidadOfertaProyecto.objects.filter(
        oferta_id__in=ids
    ).values_list("oferta_id", "proyecto_id"):
        asociadas.setdefault(oferta_id, []).append(proyecto_id)

    necesarios = {pid for lista in asociadas.values() for pid in lista}
    necesarios |= {
        o.proyecto_id for o in ofertas if o.proyecto_id and o.id not in asociadas
    }
    proyectos = _proyectos_por_id(necesarios)

    salida: dict[int, list] = {}
    for o in ofertas:
        pids = asociadas.get(o.id) or ([o.proyecto_id] if o.proyecto_id else [])
        elegidos = [proyectos[pid] for pid in pids if pid in proyectos]
        elegidos.sort(key=lambda p: ((p.nombre_comercial or "").lower(), p.id))
        if elegidos:
            salida[o.id] = elegidos
    return salida


def _opciones_proyecto() -> tuple:
    """Todo lo que la ficha de la planta va a leer, precargado por lotes.

    `prefetch_related` y no `select_related`: son colecciones (fronteras,
    inversores) y con JOIN se multiplicarían las filas del proyecto. Cada opción
    cuesta UNA consulta extra para el lote completo, no una por planta — es lo
    que protege el test de N+1: el número de consultas no depende de cuántas
    plantas traiga la respuesta.

    Sin precargarlas, armar la ficha de 60 plantas costaría ~300 consultas.
    """
    return (
        "operador_red",
        "fronteras__operador_red",
        "info_tecnica",
        "inversores",
        "portafolio",
    )


def _proyectos_con_ficha(filtro=None):
    from apps.proyectos.models import Proyecto

    qs = Proyecto.objects.filter(deleted_at__isnull=True)
    if filtro is not None:
        qs = qs.filter(filtro)
    return qs.prefetch_related(*_opciones_proyecto())


def _proyectos_por_ppa(ppa_ids: set[int]) -> dict[int, list]:
    """Las plantas de cada PPA, en una consulta para todos, con la ficha completa
    precargada (ver `_opciones_proyecto`)."""
    from apps.ppa.models import PpaContratoProyecto

    if not ppa_ids:
        return {}
    por_contrato: dict[int, list[int]] = {}
    for contrato_id, proyecto_id in PpaContratoProyecto.objects.filter(
        contrato_id__in=ppa_ids
    ).values_list("contrato_id", "proyecto_id"):
        por_contrato.setdefault(contrato_id, []).append(proyecto_id)

    proyectos = _proyectos_por_id(
        {pid for lista in por_contrato.values() for pid in lista}
    )
    return {
        cid: [proyectos[pid] for pid in pids if pid in proyectos]
        for cid, pids in por_contrato.items()
    }


def _proyectos_por_id(ids: set[int]) -> dict:
    if not ids:
        return {}
    return {p.id: p for p in _proyectos_con_ficha(Q(id__in=ids))}


def api_id_unergy(proyecto) -> tuple[str | None, str | None]:
    """Con qué id se consulta esta planta en la API de Unergy, y de qué campo
    salió."""
    if proyecto is None:
        return None, None
    if proyecto.sub_project:
        return proyecto.sub_project, "sub_project"
    return None, None


def _es_comunidad(oferta, ppa) -> bool:
    """Si este PPA es de comunidad energética."""
    if ppa is not None and ppa.es_comunidad_energetica is not None:
        return bool(ppa.es_comunidad_energetica)
    return _valor_enum(oferta.tipo) == "comunidad_energetica"


def _condiciones(oferta, ppa, hoy=None) -> dict:
    """Periodo, duración y energía del PPA, con `origen` diciendo de dónde salen.

    En cuanto el contrato existe manda el contrato, sin mezclar: una fecha de la
    oferta y una pactada se ven idénticas en el JSON, y sin `origen` quien
    integra no puede saber si está mirando una intención o un compromiso.
    """
    if ppa is not None:
        c = duracion_contrato(ppa.fecha_inicio, ppa.fecha_fin, hoy=hoy)
        c["origen"] = "contrato"
        c["energia_kwh_mes"] = (float(ppa.cantidad_minima_kwh_mes)
                                if ppa.cantidad_minima_kwh_mes is not None else None)
        return c
    c = duracion_contrato(oferta.fecha_tentativa_inicio, oferta.fecha_fin_tentativa,
                          hoy=hoy)
    c["origen"] = "oferta"
    # En la oferta la energía es una ESTIMACION técnica de la planta, no un
    # compromiso contractual como cantidad_minima_kwh_mes. Ocupa la misma casilla
    # porque es lo que se compara, y `origen` avisa que no es lo mismo.
    c["energia_kwh_mes"] = (float(oferta.energia_promedio_kwh_mes)
                            if oferta.energia_promedio_kwh_mes is not None else None)
    return c


def _operador_red(proyecto, nombre_oferta=None, id_oferta=None):
    """El operador de red de la planta, su id de catálogo y de dónde salió.

    Es `Proyecto.operador_red_legal` con el id al lado y un escalón más. El id
    hace falta para cruzar contra `operadores_red`, y devolver el nombre de la
    frontera junto al `operador_red_id` del proyecto —null justo cuando la planta
    no tiene vínculo propio— hacía que nombre e id hablaran de cosas distintas.
    Por eso `frontera` es una fuente aparte de `proyecto`.

    Requiere `operador` y `fronteras.operador` precargados (lo hacen las dos
    funciones que traen los proyectos de este módulo).
    """
    if proyecto is not None:
        if proyecto.operador_red_id and proyecto.operador_red:
            return proyecto.operador_red.nombre_legal, proyecto.operador_red_id, "proyecto"
        for f in proyecto.fronteras.all():
            if f.operador_red_id and f.operador_red:
                return f.operador_red.nombre_legal, f.operador_red_id, "frontera"
    if nombre_oferta:
        return nombre_oferta, id_oferta, "oferta"
    return None, None, None


# ── La ficha completa de la planta ───────────────────────────────────────────
# Los datos con los que se creó el Proyecto en la plataforma: cómo se identifica
# en cada sistema, su clasificación regulatoria, la ubicación fina, la ficha
# técnica, las fronteras comerciales, los servicios contratados, el pipeline de
# obra y la curva simulada.
#
# Ninguno de estos campos tiene escalón de oferta —una oferta comercial no
# declara marcas de inversor ni códigos SIC—, así que salen del Proyecto o salen
# null, y por eso NO entran a `fuentes`: ese mapa existe para los campos que se
# resuelven por cascada, y meter ahí veinte entradas con el valor "proyecto"
# fijo lo volvería ruido.
#
# Los bloques viajan siempre, aunque estén vacíos, para que quien integre lea
# una sola forma: un `tecnica` ausente y uno con todo en null se programan
# distinto, y la planta sin ficha técnica cargada es el caso normal, no un error.


def _num(v):
    """Decimal de la BD → float del JSON, dejando pasar el null."""
    return float(v) if v is not None else None


def _identificacion(proyecto) -> dict:
    """Con qué código se cruza esta planta en cada sistema.

    Van todos los identificadores juntos porque el error clásico de una
    integración es cruzar por el id equivocado: `sub_project` (API de generación
    de Unergy), `project_id_solenium` (data.sole.tech) y
    `sunfactory_project_id` (el pipeline de obra) son tres espacios de ids
    distintos aunque los tres se lean como "el id de la planta". El nombre del
    sistema está en la clave a propósito.
    """
    return {
        "nombre_comercial": proyecto.nombre_comercial,
        # Unergy (generación). Es el mismo valor que `api_id_unergy` del nodo.
        "sub_project": proyecto.sub_project,
        "codigo_cnd": proyecto.codigo_cnd,
        "codigo_tsf": proyecto.codigo_tsf,
        "origina_code": proyecto.origina_code,
        "project_id_solenium": proyecto.project_id_solenium,
        "sunfactory_project_id": proyecto.sunfactory_project_id,
        "portafolio_id": proyecto.portafolio_id,
        "portafolio": proyecto.portafolio.nombre if proyecto.portafolio else None,
    }


def _clasificacion(proyecto) -> dict:
    """Qué tipo de planta es, en los tres ejes con los que se la clasifica.

    `clasificacion_regulatoria` (AGP/AGPE/AGGE/GD/DER) es la de la CREG y es la
    que manda para el mercado; `tipo_proyecto` es la interna
    (minigranja/autoconsumo/gd/...) y `tipo_tecnologia` la fuente. Son ejes
    independientes y no se derivan uno del otro.
    """
    return {
        "clasificacion_regulatoria": _valor_enum(proyecto.clasificacion_regulatoria),
        "tipo_tecnologia": _valor_enum(proyecto.tipo_tecnologia),
        "tipo_proyecto": _valor_enum(proyecto.tipo_proyecto),
        # Ortogonal a todo lo anterior: cualquier planta puede o no pertenecer a
        # una comunidad energética.
        "es_comunidad_energetica": bool(proyecto.es_comunidad_energetica),
        "nombre_comunidad": proyecto.nombre_comunidad,
    }


def _servicios_planta(proyecto) -> dict:
    """Qué le presta Unergy a esta planta.

    Son los flags del Proyecto, que dicen qué servicio está ACTIVO — no los
    contratos que lo respaldan, que son otra entidad (`contratos_servicio`). Se
    exponen sin el prefijo `srv_` porque afuera el prefijo no significa nada.
    """
    return {
        "operacion": bool(proyecto.srv_operacion),
        "representacion": bool(proyecto.srv_representacion),
        "cgm": bool(proyecto.srv_cgm),
        "ppa": bool(proyecto.srv_ppa),
        # Hasta cuándo la representa Unergy ante el mercado. Va con los
        # servicios y no con las fechas del contrato de energía: son cosas
        # distintas y una planta puede tener representación sin PPA.
        "fecha_fin_representacion": proyecto.fecha_fin_representacion,
    }


def _ficha_tecnica(proyecto) -> dict:
    """La ficha técnica: red, paneles, inversores, equipos y almacenamiento.

    Casi todo vive en `proyecto_info_tecnica` (una fila por planta) y esa fila
    **puede no existir**: ahí sus campos salen null y el bloque viaja igual.

    `inversores.equipos` es la lista REAL cargada en la plataforma —los
    inversores son los que se usan para reportar fallas por inversor— y no el
    conteo de la ficha, que es un resumen escrito a mano y puede no coincidir.
    Los dos viajan: el conteo dice lo que declaró el diseño, la lista lo que
    está cargado. (`paneles.grupos` existió con el mismo propósito para
    paneles -- ProyectoGrupoPanel, retirado 2026-08-20 por cero adopción real
    en toda la plataforma; el conteo/marca/potencia de paneles sigue viniendo
    de proyecto_info_tecnica.)

    De los equipos salen las MARCAS y no las IPs ni las contraseñas de módem que
    están en la misma tabla: son credenciales de acceso al equipo y esta
    superficie es de consulta.
    """
    it = proyecto.info_tecnica.first()

    def _it(attr):
        return getattr(it, attr, None) if it is not None else None

    inversores = sorted(
        (i for i in proyecto.inversores.all() if i.activo),
        key=lambda i: (i.orden if i.orden is not None else 0, i.id))
    return {
        "voltaje_red": _it("voltaje_red"),
        # Potencia AC (la del punto de conexión) contra kWp instalados (DC): no
        # son el mismo número y la relación entre las dos es el sobredimensionado.
        # Del proyecto, no de la info técnica: es su única casa desde el
        # 2026-09-10 (antes había una copia espejada en las dos tablas).
        "potencia_ac_kw": _num(proyecto.potencia_ac_kw),
        "capacidad_instalada_kwp": _num(_it("capacidad_instalada_kwp")),
        "produccion_especifica_kwh_kwp": _num(proyecto.produccion_especifica_kwh_kwp),
        "tipo_tracker": _valor_enum(_it("tipo_tracker")),
        "paneles": {
            # El conteo vive SOLO en la ficha técnica. Hasta 2026-08-19 estaba
            # duplicado en `proyectos.cantidad_total_paneles` y acá se elegía uno
            # de los dos; al eliminarse esa columna duplicada este era el último
            # lector que quedaba, y leerla tiraba AttributeError en CADA planta
            # del árbol — es decir, /comercial/proyectos-operando entero.
            "cantidad_total": _it("cantidad_total_paneles"),
            "marca": _it("marca_paneles"),
            "potencia_panel_kwp": _it("potencia_panel_kwp"),
        },
        "inversores": {
            "cantidad": _it("cantidad_inversores"),
            "marca": _it("marca_inversores"),
            "potencia_kwp": _it("potencia_inversores_kwp"),
            "cantidad_strings": _it("cantidad_strings"),
            # Solo los activos: un inversor dado de baja no está en la planta, y
            # devolverlo haría que la suma de potencias no cuadre.
            "equipos": [
                {
                    "id": i.id,
                    "nombre": i.nombre,
                    "potencia_nominal_kw": _num(i.potencia_nominal_kw),
                }
                for i in inversores
            ],
        },
        "equipos_marcas": {
            "transformador": _it("marca_transformador"),
            "reconectador_rele": _it("marca_reconectador_rele"),
            "totalizador": _it("marca_totalizador"),
            "seguidor_solar": _it("marca_seguidor_solar"),
            "medidores_frontera": _it("marca_medidores_frontera"),
            "modems_frontera": _it("marca_modems_frontera"),
            "cctv": _it("marca_cctv"),
        },
        "seguridad_fisica": _it("seguridad_fisica"),
        "tiene_internet": _it("tiene_internet"),
    }


def _fronteras_planta(proyecto) -> list[dict]:
    """Las fronteras comerciales de la planta: con qué código se liquida.

    Es el dato que cruza esta API contra el SIC y contra las liquidaciones, y no
    puede vivir a nivel de PPA: una planta puede tener frontera de generación y
    de consumo, y un contrato de dos plantas tiene las de las dos.

    Se omiten las BORRADAS (`deleted_at`): la relación del modelo no filtra el
    soft delete, así que una frontera dada de baja seguiría saliendo como
    vigente. Tampoco viajan contraseñas de medidor ni IPs de módem, que están en
    la misma tabla.

    El operador de la frontera se devuelve del catálogo cuando existe el vínculo
    y, si no, el texto de GESCON —con `operador_red_id` en null justamente para
    avisar que con ese valor no se puede cruzar el catálogo.

    `capacidad_transporte_mw`/`capacidad_efectiva_mw`/`departamento`/
    `municipio`: hasta 2026-08-25 eran columnas propias de Frontera: se
    eliminaron por duplicar (con conversión de unidad para las capacidades,
    igual salvo diferencias de formato para municipio) a los campos
    homónimos de `Proyecto`, la fuente única desde entonces -- se repuntan
    acá en vez de quitarse del dict para no romper a quien integre contra
    estas claves.

    `factor_perdidas`/`es_agrupadora`: columnas de Frontera eliminadas
    2026-08-25 sin equivalente en Proyecto (no duplicaban nada, eran datos
    propios de la frontera). A diferencia de las anteriores, sí se quitan
    del dict -- confirmado que no tienen consumidor externo.
    """
    cap_mw = (
        float(proyecto.potencia_ac_kw) / 1000
        if proyecto.potencia_ac_kw is not None else None
    )
    fronteras = sorted(
        (f for f in proyecto.fronteras.all() if f.deleted_at is None),
        key=lambda f: (f.codigo_frontera or "", f.id))
    return [
        {
            "id": f.id,
            "codigo_frontera": f.codigo_frontera,
            "nombre_frontera": f.nombre_frontera,
            "tipo_frontera": _valor_enum(f.tipo_frontera),
            "estado": _valor_enum(f.estado),
            "nivel_tension_kv": _num(f.nivel_tension_kv),
            "capacidad_transporte_mw": cap_mw if f.tipo_frontera == "generacion" else None,
            "capacidad_efectiva_mw": cap_mw if f.tipo_frontera == "generacion" else None,
            "municipio": proyecto.municipio,
            "departamento": proyecto.departamento,
            "operador_red": f.operador_red.nombre_legal if f.operador_red_id else None,
            "operador_red_id": f.operador_red_id,
            "fecha_registro_asic": f.fecha_registro_asic,
        }
        for f in fronteras
    ]


def _construccion(proyecto) -> dict:
    """En qué punto de la obra está la planta (pipeline de Sun Factory).

    Complementa `estado_proyecto`, que se queda en `en_desarrollo` durante toda
    la construcción y no distingue una planta en cimientos de una que se
    energiza la semana entrante.
    """
    return {
        "fase": proyecto.fase_construccion,
        "avance_obra_pct": _num(proyecto.avance_obra_pct),
        # Siempre la que trae Sun Factory (solo lectura en la plataforma).
        "fecha_estimada_energizacion": proyecto.fecha_estimada_energizacion,
        # 'manual' (alta normal) | 'tsf_sync' (auto-importado de Sun Factory).
        "origen_registro": proyecto.origen,
    }


def _simulacion(proyecto) -> dict:
    """La curva de generación simulada: 12 valores en kWh, índice 0 = enero.

    Los tres escenarios viajan porque no son intercambiables: el P50 es el
    esperado y con el P90/P99 se estructura el negocio. Es una PROYECCIÓN del
    estudio, distinta de `energia_promedio_mensual_*`, que puede ser medida —
    `energia_promedio_origen` dice cuál de las dos se está mirando.

    `serie_mensual_kwh` absorbe que las filas viejas guarden la lista como texto
    JSON en vez de array: leerla cruda tumbaba /comercial con un 500.
    """
    def _serie(v):
        return serie_mensual_kwh(v) or None

    p50 = _serie(proyecto.p50_mensual_kwh)
    return {
        "p50_mensual_kwh": p50,
        "p90_mensual_kwh": _serie(proyecto.p90_mensual_kwh),
        "p99_mensual_kwh": _serie(proyecto.p99_mensual_kwh),
        # La suma de los 12, para no obligar a sumarla del otro lado. Solo si la
        # serie está completa: sumar 7 meses y llamarlo anual sería mentira.
        "p50_anual_kwh": (round(sum(p50), 3) if p50 and len(p50) == 12 else None),
    }


def _nodo_proyecto(proyecto, ofertas, operadores=None) -> dict:
    """Una planta del PPA con sus detalles.

    `detalles` es la ficha de la PLANTA: dónde está, quién le distribuye, qué
    potencia tiene instalada, en qué estado está y desde cuándo entrega energía.
    Son los datos con los que se cruza esta API contra otra, y viven en el
    Proyecto: el nivel PPA no los tiene ni puede tenerlos, porque un contrato de
    dos plantas no está en un municipio ni tiene un operador.

    La energía promedio sale de `_gen_promedio`, la misma cascada
    medido → estimado → declarado que usa proyectos_operando, con su origen al
    lado: un promedio medido y una proyección de ingeniería no valen lo mismo.
    NO se calcula energía acumulada acá — `api_id_unergy` es la llave para que
    quien integre la consulte contra la API de generación cuando la necesite.

    `ofertas` son las que DECLARARON esta planta, no todas las del PPA: lo que
    cae al escalón de la oferta (ubicación y energía declaradas, operador
    declarado) son afirmaciones sobre una planta concreta, y aplicárselas a una
    hermana del mismo contrato le inventaría datos. Puede venir vacía —una planta
    que el contrato cubre y ninguna oferta nombró— y ahí manda solo el Proyecto.

    `operadores` es `{operador_red_id: nombre_legal}` para el escalón de la
    oferta, que declara el id y no el nombre. La planta ya trae el suyo
    precargado y no lo necesita.

    `fuentes` dice de dónde salió cada campo. Sin ese mapa "no aplica" y
    "todavía no lo sabemos" se ven idénticos, y un operador del catálogo se lee
    igual que uno de texto libre.
    """
    from app.models.proyectos import ESTADO_PROYECTO_LABELS  # dict, no toca la BD

    ofertas = list(ofertas)
    fuentes: dict[str, str | None] = {}

    def _elegir(campo, del_proyecto, de_la_oferta):
        if del_proyecto not in (None, ""):
            fuentes[campo] = "proyecto"
            return del_proyecto
        if de_la_oferta not in (None, ""):
            fuentes[campo] = "oferta"
            return de_la_oferta
        fuentes[campo] = None
        return None

    def _declarado(attr):
        """Primer valor no vacío entre las ofertas que nombraron esta planta."""
        for o in ofertas:
            v = getattr(o, attr, None)
            if v not in (None, ""):
                return v
        return None

    # Municipio y departamento se resuelven por separado a propósito: hay filas
    # con el departamento cargado y el municipio en blanco, y colapsarlos en un
    # solo campo perdería el que sí está.
    municipio = _elegir("municipio", proyecto.municipio, _declarado("municipio"))
    departamento = _elegir("departamento", proyecto.departamento,
                           _declarado("departamento"))
    partes = [p for p in (municipio, departamento) if p]

    # El nombre del operador se busca en la MISMA oferta de la que sale el id (la
    # primera que lo declaró). Resolverlo en la primera que "exista en el
    # catálogo" podía devolver el nombre de una oferta y el id de otra.
    con_operador = next((o for o in ofertas if o.operador_red_id is not None), None)
    operador, operador_id, fuentes["operador_red"] = _operador_red(
        proyecto,
        nombre_oferta=((operadores or {}).get(con_operador.operador_red_id)
                       if con_operador else None),
        id_oferta=con_operador.operador_red_id if con_operador else None)

    # Estado del PROYECTO (en_desarrollo | en_operacion | suspendido | cancelado),
    # que no es la etapa comercial del PPA: uno dice en qué punto está la planta y
    # el otro en qué punto está el negocio. Pueden discrepar (PPA operando y
    # planta todavía en_desarrollo) y acá NO se los concilia: inventar coherencia
    # taparía el dato mal cargado en vez de mostrarlo. La etiqueta viaja al lado
    # para que quien integra la pinte tal cual.
    estado = _valor_enum(proyecto.estado)
    fuentes["estado_proyecto"] = "proyecto" if estado else None

    mwh, origen, detalle = _gen_promedio(proyecto, ofertas)
    fuentes["energia_promedio"] = origen
    sub, fuentes["api_id_unergy"] = api_id_unergy(proyecto)

    # Inicio de comercialización = primer día con generación real (lo autoderiva
    # app.services.comercializacion). NO se rellena con la fecha del contrato ni
    # con la entrada en operación: son tres hechos distintos y mezclarlos dejaría
    # el campo sin poder creerse. Los otros dos viajan aparte —la del contrato en
    # `condiciones` del PPA, la de operación acá al lado.
    fuentes["fecha_inicio_comercializacion"] = (
        "proyecto" if proyecto.fecha_inicio_comercializacion else None)

    return {
        "proyecto_id": proyecto.id,
        "nombre": proyecto.nombre_comercial,
        "api_id_unergy": sub,
        "detalles": {
            "estado_proyecto": estado,
            "estado_proyecto_label": ESTADO_PROYECTO_LABELS.get(estado),
            "potencia_ac_kw": _num(proyecto.potencia_ac_kw),
            # Potencia con CEN, en MW. Va aparte de la instalada y no se convierte
            # a una sola unidad: no son el mismo número ni salen del mismo papel.
            "potencia_con_cen_mw": _num(proyecto.potencia_con_cen_mw),
            "ubicacion": {
                "municipio": municipio,
                "departamento": departamento,
                # Armado acá para que quien integre no decida el orden ni el
                # separador; null cuando no hay ninguna de las dos partes.
                "texto": ", ".join(partes) if partes else None,
                "latitud": _num(proyecto.latitud),
                "longitud": _num(proyecto.longitud),
                # Dirección o vereda: la ubicación fina, que en zona rural es lo
                # único que hay además de las coordenadas.
                "direccion": proyecto.direccion_vereda,
                # El enlace al mapa que cargó operaciones (Google Maps u otro).
                # No se genera uno a partir de lat/lon: si está en null es que
                # nadie lo cargó, y fabricarlo taparía la diferencia.
                "url_mapa": (it.url_ubicacion if (it := proyecto.info_tecnica.first()) else None),
            },
            "operador_red": operador,
            # Id del catálogo `operadores_red`, para cruzar. Null cuando el
            # nombre salió de lo declarado en una oferta sin operador propio
            # (`fuentes.operador_red == "oferta"`).
            "operador_red_id": operador_id,
            "fecha_entrada_operacion": proyecto.fecha_entrada_operacion,
            "fecha_inicio_comercializacion": proyecto.fecha_inicio_comercializacion,
            # El promedio va en las dos unidades a propósito: la plataforma habla
            # en MWh y el CRM en kWh, y la conversión hecha en dos integraciones
            # distintas es un factor 1000 esperando pasar.
            "energia_promedio_mensual_mwh": mwh,
            "energia_promedio_mensual_kwh": round(mwh * 1000, 3) if mwh is not None else None,
            "energia_promedio_origen": origen,
            "energia_promedio_detalle": detalle,
            # ── El resto de la ficha del Proyecto ─────────────────────────────
            # Todo lo que se diligencia al crear una planta en la plataforma.
            # Sale del Proyecto o sale null (no hay escalón de oferta para nada
            # de esto), y por eso no aparece en `fuentes`.
            "identificacion": _identificacion(proyecto),
            "clasificacion": _clasificacion(proyecto),
            "tecnica": _ficha_tecnica(proyecto),
            "fronteras": _fronteras_planta(proyecto),
            "servicios": _servicios_planta(proyecto),
            "construccion": _construccion(proyecto),
            "simulacion": _simulacion(proyecto),
        },
        "fuentes": fuentes,
    }


def _resolver_ppas(ofertas, de_oferta, hoy) -> dict[int, tuple]:
    """El contrato de cada oferta y de dónde salió: `(ppa, fuente)`.

    Dos caminos, y el explícito manda:

    1. `oferta.ppa_contrato_id` — el enlace que deja `firmar`. Fuente `"oferta"`.
    2. El mejor PPA de las PLANTAS de la oferta. Fuente `"proyecto"`.

    El segundo camino no es un lujo: en producción **ningún** PPA está enlazado a
    su oferta, porque los contratos son anteriores al CRM. Sin él, una planta con
    contrato salía como `sin_contrato` —que significa "falta cargarlo"— y eso es
    una falsa alarma.

    `(None, None)` cuando no hay contrato por ningún camino: ahí la alarma es real.
    """
    enlazados = _ppas_por_id({o.ppa_contrato_id for o in ofertas if o.ppa_contrato_id})

    # Solo se buscan los PPAs de las plantas de las ofertas que quedaron sin
    # enlace: no tiene sentido pagar la consulta por las que ya lo declararon.
    sin_enlace = [o for o in ofertas if enlazados.get(o.ppa_contrato_id) is None]
    por_proyecto = _ppas_de_proyectos(
        {p.id for o in sin_enlace for p in de_oferta.get(o.id, [])}
    )

    salida = {}
    for o in ofertas:
        ppa = enlazados.get(o.ppa_contrato_id)
        if ppa is not None:
            salida[o.id] = (ppa, "oferta")
            continue
        candidatos = [
            c for p in de_oferta.get(o.id, []) for c in por_proyecto.get(p.id, [])
        ]
        mejor = _mejor_ppa(candidatos, hoy)
        salida[o.id] = (mejor, "proyecto" if mejor is not None else None)
    return salida


def _ppas_de_proyectos(proyecto_ids: set[int]) -> dict[int, list]:
    """Los contratos de energía de cada planta, en una consulta para todas."""
    from apps.ppa.models import PpaContratoProyecto

    if not proyecto_ids:
        return {}
    salida: dict[int, list] = {}
    for vinculo in (
        PpaContratoProyecto.objects
        .filter(proyecto_id__in=proyecto_ids, contrato__deleted_at__isnull=True)
        .select_related("contrato")
    ):
        salida.setdefault(vinculo.proyecto_id, []).append(vinculo.contrato)
    return salida


def _operadores_por_id(ids: set[int]) -> dict[int, str]:
    """Nombre legal de los operadores declarados en las ofertas, en una consulta.

    Solo hace falta para el escalón `oferta`: la oferta declara el id del catálogo
    y no el nombre. Las plantas ya traen su operador precargado.
    """
    from apps.fronteras.models import OperadorRed

    if not ids:
        return {}
    return dict(
        OperadorRed.objects.filter(id__in=ids).values_list("id", "nombre_legal")
    )


def _clientes_por_oportunidad(ids: set[int]) -> dict[int, str]:
    """Razón social del dueño del negocio, por oportunidad, en una consulta."""
    from apps.comercial.models import Oportunidad

    if not ids:
        return {}
    return dict(
        Oportunidad.objects.filter(id__in=ids)
        .values_list("id", "cliente__razon_social_nombre")
    )


def _ppas_por_id(ids: set[int]) -> dict:
    """Los PPAs materializados, en una sola consulta. Un PPA borrado deja a su
    oferta como borrador: la fila ya no está, así que el contrato tampoco."""
    from apps.ppa.models import PpaContrato

    if not ids:
        return {}
    return {
        c.id: c for c in PpaContrato.objects.filter(
            id__in=ids, deleted_at__isnull=True,
        )
    }


def _nodo_ppa(oferta, ppa=None, fuente_ppa=None, proyectos=(), ofertas=None,
              cliente=None, hoy=None, declarantes=None, operadores=None) -> dict:
    """Un PPA (borrador o materializado) con sus plantas.

    `ppa is None` ⟺ borrador: la oferta todavía no desembocó en un contrato. No
    se inventa un id ni se rellenan los campos del contrato con los de la
    oferta sin decirlo — el borrador declara sus condiciones como tentativas.
    """
    etapa = _valor_enum(oferta.estado)
    return {
        "ppa": {
            "id": None if ppa is None else ppa.id,
            # UN solo estado, y es el del pipeline comercial: oportunidad,
            # oferta, contrato, firmado, operando, terminado, declinado.
            #
            # Antes había dos (`etapa_comercial` y `estado_ppa`) y el segundo no
            # aportaba nada: era función pura de este estado y de si `id` es
            # None. Peor, reusaba la palabra «firmado» con OTRO significado —
            # "existe la fila en ppa_contratos" — así que un nodo podía decir
            # `estado_ppa: firmado` junto a `etapa_comercial: oferta` y leerse
            # como una contradicción cuando no lo era.
            #
            # Lo que decían los tres valores viejos se lee de `estado` + `id`:
            #   borrador      → estado en (oferta, contrato) e `id` None
            #   firmado       → `id` con valor (el contrato existe)
            #   sin_contrato  → estado en (firmado, operando) e `id` None,
            #                   que es la INCONSISTENCIA: el negocio cerró y el
            #                   PPA no está cargado. Sigue viéndose igual de
            #                   claro, y ahora sin dos vocabularios en pugna.
            "estado": etapa,
            # El gate explícito, y el mismo hecho que `id is not None`:
            # /servicios lista `ppa_contratos`, así que sin fila no hay nada que
            # listar. Viaja como booleano para que quien integre no tenga que
            # deducirlo.
            "aparece_en_servicios": ppa is not None,
            "numero_codigo_contrato": None if ppa is None else ppa.numero_codigo_contrato,
            "nombre_interno": None if ppa is None else ppa.nombre_interno,
            # El nombre de planta tal como lo declaró la oferta. Viaja aunque el
            # nodo tenga proyectos: en el 74% del pipeline la planta todavía no
            # existe como Proyecto, y este es el único nombre que hay.
            "planta_declarada": oferta.planta_nombre,
            "condiciones": _condiciones(oferta, ppa, hoy=hoy),
            # Del contrato en cuanto existe; del tipo de la oferta mientras es
            # borrador. Nunca se deriva de la oferta si el contrato ya lo dice.
            "es_comunidad_energetica": _es_comunidad(oferta, ppa),
            "cantidad_proyectos": len(proyectos),
            # De qué camino salió el contrato: `oferta` = enlace explícito de la
            # firma, `proyecto` = el PPA vigente de la planta, `null` = no hay.
            "fuente_ppa": fuente_ppa,
            # TODAS las ofertas que desembocan en este contrato, cada una con su
            # código y su etapa. Lo normal es una sola.
            "ofertas": [_oferta_min(o) for o in (ofertas or [oferta])],
            "cliente": cliente,
            "oferta_id": oferta.id,
            "codigo_seguimiento": _codigo_seguimiento(oferta.numero_oferta),
            "oportunidad_id": oferta.oportunidad_id,
            "tipo_contrato": None if ppa is None else ppa.tipo_contrato,
            "comprador": None if ppa is None else ppa.comprador_nombre,
            "vendedor": None if ppa is None else ppa.vendedor_nombre,
        },
        # A cada planta se le pasan las ofertas que LA nombraron —no todas las
        # del contrato— para que los escalones que caen en la oferta no le
        # atribuyan a una planta lo que se declaró de su hermana.
        "proyectos": [_nodo_proyecto(p, (declarantes or {}).get(p.id, ()),
                                     operadores=operadores)
                      for p in proyectos],
    }