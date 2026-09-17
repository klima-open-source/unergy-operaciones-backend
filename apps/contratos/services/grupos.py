"""Catálogo de grupos de servicio: qué grupo, qué subservicios, qué tarifa.

Fuente única de la agrupación que la vista Servicios muestra en tres pestañas.
El razonamiento completo y la ruta están en `docs/SERVICIOS_AGRUPACION.md`.

**Un contrato cubre uno o más subservicios, todos del mismo grupo**, y ese "uno
o más" es siempre UNO salvo en `representacion_cgm`:

- `ppa` — un contrato, de compra O de venta. La tabla es otra (`ppa_contratos`)
  y el subservicio sale de su `tipo_contrato`.
- `representacion_cgm` — un contrato puede cubrir los dos subservicios a la vez
  (el caso general), uno solo, o existir un contrato por cada uno. Es el ÚNICO
  caso con más de un subservicio, y por eso `subservicios_de()` devuelve lista.
- `operacion` — cada subservicio tiene su PROPIO contrato, siempre. Una planta
  con mantenimiento, arriendo e internet tiene tres contratos distintos.

Por qué los subservicios de `representacion_cgm` se derivan de las tarifas y no
de `servicio_aplica`: ese campo admite un solo valor, así que un contrato que es
representación Y CGM tiene que mentir, y el subservicio que no quedó grabado se
vuelve invisible para todo filtro. Las cuatro columnas (`tarifa_*`,
`indexacion_*`) sí conviven en la fila, así que el dato está; lo que falta es la
etiqueta. Ver `docs/SERVICIOS_AGRUPACION.md` §5: esto es la "Opción A", y se
eligió sobre una tabla `contrato_subservicio` porque no necesita migración ni
escritura en el wizard -- y la revisión 118 de Alembic ya mostró qué le pasa a
una estructura que nada llena.

`servicio_aplica` NO se toca: los ~15 filtros literales repartidos por `apps/`
siguen funcionando igual.
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


def subservicios_de(contrato) -> list[str]:
    """Los subservicios que cubre un `ContratoServicio`.

    Devuelve **lista** porque un contrato de representación+CGM cubre dos. Para
    Operación siempre trae uno: ahí cada subservicio tiene su propio contrato.

    En `representacion_cgm` el valor NO sale de `servicio_aplica` —que solo
    admite uno— sino de qué tarifas tiene la fila. Si no tiene ninguna, se cae a
    `servicio_aplica` para no devolver vacío: un contrato recién creado, todavía
    sin tarifas cargadas, debe seguir apareciendo en su pestaña.
    """
    aplica = contrato.servicio_aplica
    if grupo_de(aplica) != REPRESENTACION_CGM:
        # Operación: uno y solo uno. Un valor desconocido devuelve lista vacía
        # en vez de inventar un grupo.
        return [aplica] if aplica in SUBSERVICIOS_DE_CONTRATO_SERVICIO else []

    encontrados = [
        sub for sub in SUBSERVICIOS[REPRESENTACION_CGM]
        if getattr(contrato, COLUMNA_TARIFA[sub], None) is not None
    ]
    return encontrados or [aplica]


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


def subservicio_de_ppa(contrato_ppa) -> str:
    """Compra o venta, desde `PpaContrato.tipo_contrato`.

    La columna admite nulo y su default es `venta`; un nulo se trata como venta
    igual que el default, en vez de dejar el PPA sin subservicio y fuera de todo
    filtro.
    """
    return COMPRA if contrato_ppa.tipo_contrato == COMPRA else VENTA


def catalogo() -> list[dict]:
    """El catálogo completo, como lo expone la API.

    El front lo consume en vez de mantener su propia lista de grupos: una sola
    definición para los dos lados.
    """
    return [
        {"grupo": grupo, "subservicios": list(SUBSERVICIOS[grupo])}
        for grupo in ORDEN_GRUPOS
    ]
