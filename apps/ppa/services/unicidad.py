"""¿Ya existe este PPA? El aviso que faltaba, igual que en contratos de servicio.

`ContratoServicio` avisa desde el 2026-09-19
(`apps/contratos/services/unicidad.py`); el PPA no, y era el único de los tres
grupos sin protección. Mismo contrato de API: 409 con el candidato y
`?forzar=true` para crear de todos modos.

## Qué cuenta como duplicado

Dos señales, en este orden:

1. **El mismo número de contrato**, normalizado. Es la identidad del documento y
   la señal más fuerte: dos filas con `UNERGY-PPA-014` son la misma. La columna
   no es UNIQUE en la base --y no se vuelve UNIQUE acá: hay 33 contratos en
   producción y 10 sin planta, así que una restricción dura podría rechazar
   filas legítimas que ya existen--.

2. **La misma contraparte, el mismo tipo y al menos una planta en común.** Cubre
   el PPA que se registra dos veces sin número, o con el número escrito
   distinto.

Las dos exigen que el otro contrato esté **vivo**: un PPA terminado no estorba a
su reemplazo. La vigencia sale de `vigencia.filtro_ppa_vivos`, que es la misma
que usan Cumplimiento y la regla de comunidades --`PpaContrato` no tiene columna
`estado`, así que su vigencia se lee solo de las fechas--.

**El tipo importa**: una planta puede tener a la vez un PPA de compra y uno de
venta, y son contratos distintos. Compararlos sin mirar `tipo_contrato` avisaría
sobre algo legítimo.

## Lo que este módulo NO cubre

`firmar()` del CRM crea PPA por su cuenta (a través de `crear_ppa`) y no pasa por
acá: el CRM no tiene pantalla donde confirmar "crear igual", así que bloquearlo
dejaría una firma sin salida. Si más adelante se quiere cubrir, lo correcto es
llamar a `buscar_duplicado` desde `crear_ppa` y sumar el resultado a
`Resultado.avisos`, que es el canal que el CRM ya sabe mostrar.
"""

from datetime import date

from django.db.models import Q

from apps.contratos.services import contrato_partes, vigencia
from apps.contratos.services.representacion_dedup import norm
from apps.ppa.models import PpaContrato, PpaContratoProyecto


def _plantas_de(contrato_ids) -> dict:
    """`{contrato_id: {proyecto_id}}` para varios contratos, en UNA consulta."""
    salida: dict = {}
    filas = PpaContratoProyecto.objects.filter(
        contrato_id__in=contrato_ids
    ).values_list("contrato_id", "proyecto_id")
    for contrato_id, proyecto_id in filas:
        salida.setdefault(contrato_id, set()).add(proyecto_id)
    return salida


def _misma_contraparte(datos: dict, existente) -> bool:
    """La parte que NO es Unergy, comparada por cliente y si no por nombre."""
    for rol in ("comprador", "vendedor"):
        # La API manda el cliente (`comprador`); el CRM y los comandos, el id.
        nuevo_id = datos.get(f"{rol}_id") or getattr(datos.get(rol), "id", None)
        viejo_id = getattr(existente, f"{rol}_id", None)
        if nuevo_id and viejo_id and nuevo_id == viejo_id:
            return True

        izq = norm(datos.get(f"{rol}_nombre"))
        der = norm(getattr(existente, f"{rol}_nombre", None))
        if izq and der and izq == der:
            return True
    return False


def buscar_duplicado(*, datos: dict, proyecto_ids, hoy: date, excluir_id=None):
    """El PPA vivo que ya cubre esto, o `None`.

    `datos` son las columnas del contrato que se quiere crear o dejar
    (`numero_codigo_contrato`, `tipo_contrato`, `comprador_id`/`_nombre`,
    `vendedor_id`/`_nombre`), y `proyecto_ids` sus plantas.
    """
    vivos = PpaContrato.objects.filter(
        vigencia.filtro_ppa_vivos(hoy), deleted_at__isnull=True,
    ).prefetch_related(contrato_partes.CON_PARTES)
    if excluir_id:
        vivos = vivos.exclude(pk=excluir_id)

    numero = norm(datos.get("numero_codigo_contrato"))
    if numero:
        for existente in vivos:
            if norm(existente.numero_codigo_contrato) == numero:
                return existente

    plantas = set(proyecto_ids or [])
    if not plantas:
        return None

    tipo = datos.get("tipo_contrato")
    candidatos = [
        c for c in vivos
        if (c.tipo_contrato or "venta") == (tipo or "venta")
        and _misma_contraparte(datos, c)
    ]
    if not candidatos:
        return None

    por_contrato = _plantas_de([c.id for c in candidatos])
    for existente in candidatos:
        if por_contrato.get(existente.id, set()) & plantas:
            return existente
    return None


def descripcion(contrato) -> str:
    """Cómo se nombra el PPA en el aviso, con lo que tenga."""
    return (
        contrato.numero_codigo_contrato
        or contrato.nombre_interno
        or f"Contrato PPA {contrato.id}"
    )
