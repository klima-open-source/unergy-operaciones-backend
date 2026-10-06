"""Catálogo de grupos de servicio: qué grupo, qué subservicios, qué tarifa.

Fuente única de la agrupación que la vista Servicios muestra en tres pestañas.
El razonamiento completo y la ruta están en `docs/SERVICIOS_AGRUPACION.md`.

**Un contrato cubre uno o más subservicios, todos del mismo grupo**, y ese "uno
o más" es siempre UNO salvo en `representacion_cgm`:

- `ppa` — un contrato, de compra O de venta (de su `tipo_contrato`).
- `representacion_cgm` — un contrato puede cubrir los dos subservicios a la vez
  (el caso general), uno solo, o existir un contrato por cada uno. Es el ÚNICO
  caso con más de un subservicio, y por eso `subservicios_de()` devuelve lista.
- `operacion` — cada subservicio tiene su PROPIO contrato, siempre. Una planta
  con mantenimiento, arriendo e internet tiene tres contratos distintos.

**Qué servicios cubre un contrato se REGISTRA en la tabla `servicios`** (plan
`docs/refactor/08-plan-django-contratos.md`), y `subservicios_de()` la lee. Antes
se deducía de las tarifas cargadas ("Opción A" de `docs/SERVICIOS_AGRUPACION.md`
§5): `servicio_aplica` admite un solo valor, así que un contrato de representación
Y CGM solo se delataba por tener las dos tarifas. Esa deducción se usa ya una sola
vez, en la copia inicial a la tabla única; la tabla la escribe únicamente
`apps/contratos/services/servicios.py`, desde `Contrato.save()`.

`servicio_aplica` NO se toca: los ~15 filtros literales repartidos por `apps/`
siguen funcionando igual, y es siempre uno de los servicios del contrato.
"""

from decimal import Decimal

# ── Grupos ────────────────────────────────────────────────────────────────
# Estas claves viajan en la API y en las URLs del front: cambiarlas rompe la
# vista Servicios. `representacion_cgm` lleva las dos palabras a propósito --
# `representacion` a secas nombraría al grupo Y a uno de sus subservicios.
PPA = "ppa"
REPRESENTACION_CGM = "representacion_cgm"
OPERACION = "operacion"

#: Orden en que se presentan las pestañas. No es alfabético: es el orden que ya
#: tiene el front (PPAView, RepresentacionView, OperacionView).
ORDEN_GRUPOS = (PPA, REPRESENTACION_CGM, OPERACION)

# ── Subservicios ──────────────────────────────────────────────────────────
COMPRA = "compra"
VENTA = "venta"
REPRESENTACION = "representacion"
CGM = "cgm"
MANTENIMIENTO = "mantenimiento"
ARRIENDO = "arriendo"
INTERNET = "internet"

#: Grupo → sus subservicios, en orden de presentación.
#:
#: Los tres de `operacion` cuentan por igual (confirmado por Sara el 2026-09-17):
#: una planta "tiene operación" si tiene contrato de mantenimiento, de arriendo
#: O de internet. Importa porque de ahí sale `srv_operacion`, que es el
#: interruptor del monitoreo: si el arriendo no contara, tres plantas que hoy
#: solo tienen ese contrato quedarían sin respaldo.
SUBSERVICIOS: dict[str, tuple[str, ...]] = {
    PPA: (COMPRA, VENTA),
    REPRESENTACION_CGM: (REPRESENTACION, CGM),
    OPERACION: (MANTENIMIENTO, ARRIENDO, INTERNET),
}

#: Subservicio → grupo. Derivado de `SUBSERVICIOS` para que no haya dos listas
#: que alguien tenga que acordarse de sincronizar.
GRUPO_DE_SUBSERVICIO: dict[str, str] = {
    sub: grupo for grupo, subs in SUBSERVICIOS.items() for sub in subs
}

#: Los subservicios que viven en `contratos_servicio.servicio_aplica`. `compra` y
#: `venta` no están: esos salen de `ppa_contratos.tipo_contrato`.
SUBSERVICIOS_DE_CONTRATO_SERVICIO = frozenset(
    SUBSERVICIOS[REPRESENTACION_CGM] + SUBSERVICIOS[OPERACION]
)

# ── Tarifas ───────────────────────────────────────────────────────────────
#: Subservicio → columna de `ContratoServicio` con su tarifa.
#:
#: Reemplaza la cadena de `if` de `apps/clientes/services/vistas.py`. Los tres
#: subservicios de Operación comparten `tarifa_base`: la tabla no tiene una
#: columna por cada uno.
COLUMNA_TARIFA: dict[str, str] = {
    REPRESENTACION: "tarifa_representacion",
    CGM: "tarifa_cgm",
    MANTENIMIENTO: "tarifa_base",
    ARRIENDO: "tarifa_base",
    INTERNET: "tarifa_base",
}

#: Subservicio → columna con su calendario de indexación, donde exista.
COLUMNA_INDEXACION: dict[str, str] = {
    REPRESENTACION: "indexacion_representacion",
    CGM: "indexacion_cgm",
}


def grupo_de(subservicio: str | None) -> str | None:
    """El grupo al que pertenece un subservicio, o `None` si no se reconoce."""
    return GRUPO_DE_SUBSERVICIO.get(subservicio or "")


#: Orden de presentación de todos los subservicios: el del catálogo.
ORDEN_SUBSERVICIOS: tuple[str, ...] = tuple(
    sub for grupo in ORDEN_GRUPOS for sub in SUBSERVICIOS[grupo]
)


def en_orden(subservicios) -> list[str]:
    """Los subservicios dados, en el orden del catálogo."""
    dados = set(subservicios)
    return [sub for sub in ORDEN_SUBSERVICIOS if sub in dados]


def subservicios_de(contrato) -> list[str]:
    """Los subservicios que cubre un contrato, según la tabla `servicios`.

    Devuelve **lista** porque un contrato de representación+CGM cubre dos. Para
    precargarlos en un listado: `prefetch_related("servicios")`.

    Un contrato todavía sin guardar (o sin filas, que no debería pasar: el
    `save()` las escribe) cae a `servicio_aplica`, para no desaparecer de su
    pestaña. Un valor fuera del catálogo devuelve lista vacía en vez de inventar.
    """
    if getattr(contrato, "pk", None) is not None:
        registrados = [s.servicio for s in contrato.servicios.all()]
        if registrados:
            return en_orden(registrados)
    aplica = getattr(contrato, "servicio_aplica", None)
    return [aplica] if aplica in SUBSERVICIOS_DE_CONTRATO_SERVICIO else []


def grupo_de_contrato(contrato) -> str | None:
    """El grupo de un `ContratoServicio`. `None` si `servicio_aplica` no se reconoce."""
    return grupo_de(contrato.servicio_aplica)


def tarifa_de(contrato, subservicio: str) -> Decimal | None:
    """La tarifa que aplica a un subservicio dentro de un contrato.

    Un contrato de representación+CGM tiene una tarifa por cada uno, así que hay
    que decir cuál se quiere -- no existe "la tarifa del contrato".
    """
    columna = COLUMNA_TARIFA.get(subservicio)
    return getattr(contrato, columna, None) if columna else None


def tarifas_de(contrato) -> dict[str, Decimal | None]:
    """Todas las tarifas del contrato, por subservicio. Para la API."""
    return {sub: tarifa_de(contrato, sub) for sub in subservicios_de(contrato)}


def subservicio_de_tipo_contrato(tipo_contrato: str | None) -> str:
    """Compra o venta, desde `tipo_contrato`. Nulo = venta, como su default: un PPA
    nunca queda sin subservicio y fuera de todo filtro."""
    return COMPRA if tipo_contrato == COMPRA else VENTA


def subservicio_de_ppa(contrato_ppa) -> str:
    """Compra o venta de un PPA, según su fila en `servicios` (la escribe
    `servicios.registrar` desde `tipo_contrato`, y nunca por separado)."""
    if getattr(contrato_ppa, "pk", None) is not None:
        for s in contrato_ppa.servicios.all():
            if s.servicio in SUBSERVICIOS[PPA]:
                return s.servicio
    return subservicio_de_tipo_contrato(getattr(contrato_ppa, "tipo_contrato", None))


def filtro_subservicio(subservicio: str):
    """El mismo criterio de `subservicios_de()`, como condición de ORM.

    Existe porque `subservicios_de()` trabaja sobre un objeto ya traído y no
    sirve dentro de un `filter()`. Sin esta cara, cada módulo escribe el criterio
    a mano -- y lo que escribían era `servicio_aplica="representacion"`, que NO
    distingue representación de CGM: en ese grupo la etiqueta nombra al grupo. Un
    contrato que solo cubre CGM entraba en los filtros de representación.

    Desde el corte a la tabla única los dos leen lo mismo: la fila de ese
    servicio en `servicios`, que se escribe al guardar el contrato. Antes se
    deducía de las tarifas cargadas.
    """
    from django.db.models import Q

    return Q(servicios__servicio=subservicio)


def catalogo() -> list[dict]:
    """El catálogo completo, como lo expone la API.

    El front lo consume en vez de mantener su propia lista de grupos: una sola
    definición para los dos lados.
    """
    return [
        {"grupo": grupo, "subservicios": list(SUBSERVICIOS[grupo])}
        for grupo in ORDEN_GRUPOS
    ]
