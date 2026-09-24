"""Las reglas de versiones de una reliquidación (§4.9 de la guía).

Cuando XM publica una versión corregida de un mes, el ciclo se repite con ella.
El paso que lo habilita —`xm_invoice_duplication_to_settlement`— copia las
facturas de XM de la versión vieja a la nueva. Sin ese paso, Repartir responde
400 («no hay facturas del período») aunque FTP y Liquidar hayan corrido bien en
la versión nueva: las facturas siguen existiendo solo en la vieja.

Las reglas se validan acá, antes de llamar a la API, por dos razones: el error
sale en español y en el acto, y una duplicación al revés (de `tx4` a `tx3`)
crearía facturas en una versión ya cerrada.
"""
from __future__ import annotations

# El orden es el de la guía: `txf` arranca el mes y de ahí en adelante son las
# reliquidaciones que publica XM. `txr` y `tx2` no salen en la guía pero existen
# en el catálogo, y van donde les corresponde por nombre.
VERSIONES = ("txf", "txr", "tx2", "tx3", "tx4", "tx5", "tx6", "tx7", "tx8")

VERSION_INICIAL = VERSIONES[0]


def validar_versiones(last_version: str | None, new_version: str | None) -> None:
    """Levanta `ValueError` si el par de versiones no tiene sentido.

    No devuelve nada: o pasa, o explica por qué no.
    """
    vieja = (last_version or "").strip()
    nueva = (new_version or "").strip()

    if nueva not in VERSIONES:
        raise ValueError(
            f"La versión «{new_version}» no existe. Las válidas son: "
            + ", ".join(VERSIONES)
        )

    if not vieja:
        # Sin versión previa no se está reliquidando: se está arrancando el mes.
        if nueva != VERSION_INICIAL:
            raise ValueError(
                f"Sin versión anterior, la nueva tiene que ser «{VERSION_INICIAL}»: "
                f"no hay facturas que duplicar hacia «{nueva}»."
            )
        return

    if vieja not in VERSIONES:
        raise ValueError(
            f"La versión «{last_version}» no existe. Las válidas son: "
            + ", ".join(VERSIONES)
        )

    if vieja == nueva:
        raise ValueError(
            "La versión nueva tiene que ser distinta de la anterior: duplicar "
            f"«{vieja}» sobre sí misma no reliquida nada."
        )

    if VERSIONES.index(vieja) > VERSIONES.index(nueva):
        raise ValueError(
            f"«{nueva}» es anterior a «{vieja}». La reliquidación va hacia "
            "adelante: la versión nueva tiene que ser posterior."
        )
