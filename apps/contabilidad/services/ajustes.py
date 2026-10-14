"""El panel de AJUSTE de una reliquidación: solo lo que vuelve a contabilizarse.

Cuando XM publica una versión corregida de un mes, el estado de resultados que se
emite para esa reliquidación **ya viene con la diferencia** contra la versión
anterior: no hay que restar nada acá. Lo que cambia es QUÉ de ese ER entra al
panel.

Un ajuste contabiliza únicamente **ingresos y comercialización**. El OPEX no
entra —los costos operativos no se reliquidan, se causaron una vez y ahí
quedaron— y las facturas de servicio tampoco: el ajuste no emite factura.
Guardar esas líneas con el valor de la diferencia haría que la vista las sumara
como si hubiera que volver a causarlas.

El tipo del panel lleva la versión (`ajuste_tx3`) para que un mes reliquidado dos
veces tenga un panel por cada una: la unicidad de `panel_contable` es
`(periodo, proyecto, tipo)`.

Todo acá es puro —sin base de datos— para poder probarlo: `tests/` no tiene
`pytest-django` y un test que toque el ORM no corre.
"""
from __future__ import annotations

from apps.liquidaciones.services import reliquidacion

TIPO_PRELIQUIDACION = "preliquidacion"
TIPO_OFICIAL = "oficial"

#: Prefijo de los paneles de ajuste. `ajuste_tx3` son 10 caracteres y la columna
#: `panel_contable.tipo` es `varchar(20)`: cabe sin migración.
PREFIJO_AJUSTE = "ajuste_"

#: Los únicos grupos de línea que entran a un panel de ajuste.
#: Los otros dos que existen —`costos` y `facturas`— se descartan a propósito.
GRUPOS_AJUSTE = ("ingresos", "comercializacion")

#: Una reliquidación es de `tx3` en adelante (regla de negocio, Jessica
#: 2026-10-09). `txf` es la liquidación inicial del mes y `txr`/`tx2` son pasos
#: previos a ella: ninguno genera un ajuste contable, porque no hay nada
#: contabilizado antes contra lo cual ajustar.
PRIMERA_VERSION_RELIQUIDACION = "tx3"

VERSIONES_AJUSTE = reliquidacion.VERSIONES[
    reliquidacion.VERSIONES.index(PRIMERA_VERSION_RELIQUIDACION):
]


def tipo_de_ajuste(version: str) -> str:
    """`'tx3'` → `'ajuste_tx3'`. No valida: eso lo hace `tipo_valido`."""
    return PREFIJO_AJUSTE + (version or "").strip().lower()


def version_de_ajuste(tipo: str) -> str | None:
    """La versión que lleva un tipo de ajuste, o `None` si no es uno."""
    t = (tipo or "").strip().lower()
    if not t.startswith(PREFIJO_AJUSTE):
        return None
    version = t.removeprefix(PREFIJO_AJUSTE)
    return version if version in VERSIONES_AJUSTE else None


def es_ajuste(tipo: str) -> bool:
    """¿Este panel es el ajuste de una reliquidación?"""
    return version_de_ajuste(tipo) is not None


def tipos_de_ajuste() -> tuple[str, ...]:
    """Todos los tipos de ajuste posibles, para nombrarlos en un error."""
    return tuple(tipo_de_ajuste(v) for v in VERSIONES_AJUSTE)


def tipo_valido(tipo: str) -> bool:
    """Los tipos de panel que existen.

    Se valida en vez de dejar el campo libre porque `tipo` es la llave por la que
    cada vista filtra: un `ajsute_tx3` mal escrito se guardaría sin queja y
    crearía un panel que nadie vuelve a ver nunca.
    """
    t = (tipo or "").strip().lower()
    return t in (TIPO_PRELIQUIDACION, TIPO_OFICIAL) or es_ajuste(t)


def error_de_tipo(tipo: str) -> str:
    """El mensaje que explica por qué `tipo` no sirve."""
    return (
        f"tipo «{tipo}» no existe. Válidos: «{TIPO_PRELIQUIDACION}», "
        f"«{TIPO_OFICIAL}», o un ajuste de reliquidación: "
        + ", ".join(f"«{t}»" for t in tipos_de_ajuste())
    )


def solo_lineas_de_ajuste(lineas: list[dict]) -> list[dict]:
    """Deja únicamente las líneas que un ajuste contabiliza.

    Conserva el orden original: la vista lo usa para mostrar los ingresos antes
    que la comercialización, igual que el panel oficial.

    Las líneas en cero NO se descartan. Un concepto que la reliquidación dejó
    igual es información —se revisó y no cambió— y botarlo haría imposible
    distinguirlo de uno que la versión nueva simplemente no trajo.
    """
    return [l for l in lineas if l.get("grupo") in GRUPOS_AJUSTE]
