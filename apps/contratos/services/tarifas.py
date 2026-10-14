"""La tarifa de cada servicio de un contrato: su base, su índice y su valor por periodo.

Esquema de Sara (2026-10-14):

- `servicios.tarifa_base`: el precio pactado. `servicios.tipo_indexacion`: IPC o IPP;
  vacío = no se indexa (la administración, un PPA de precio fijo).
- `tarifas` (`servicio`, `anio`, `mes`, `valor`): lo que vale en cada periodo. `mes`
  va vacío si el servicio se cobra por año, y lleno en los PPA, cuyo precio cambia
  mes a mes.

Reemplaza a las columnas sueltas de `contratos` (`tarifa_representacion`,
`tarifa_cgm`, `tarifa_admin`, `tarifa_base`, `tarifa_mensual`), a los cuatro
historiales JSON (`indexacion_*`), donde un "5" no decía si eran $/kWh o 5 %, y a
`ppa_tarifas`.

**Paso 1 (este):** las columnas y `ppa_tarifas` siguen siendo lo que escriben las
pantallas, y `sincronizar` deriva de ellas el servicio y sus filas: lo llaman
`Contrato.save()` y quien reemplaza las filas de `ppa_tarifas`.
`registrar_tarifas` lo hace una vez para los contratos que ya estaban. Mismo patrón
que `contrato_partes` y `contrato_proyectos`.

De dónde sale cada servicio:

| Servicio        | Base                     | Valor por periodo                 | Unidad |
|-----------------|--------------------------|-----------------------------------|--------|
| representacion  | `tarifa_representacion`  | `indexacion_representacion` (año) | $/kWh  |
| cgm             | `tarifa_cgm`             | `indexacion_cgm` (año)            | $/kWh  |
| administracion  | `tarifa_admin`           | — (es un % fijo)                  | %      |
| mantenimiento   | `tarifa_base` (anual)    | — (lo calcula el panel O&M)       | $/año  |
| arriendo        | `tarifa_base` (anual)    | — (lo calcula arriendos)          | $/año  |
| internet        | `tarifa_mensual`         | `indexacion_mensual` (año)        | $/mes  |
| compra / venta  | `tarifa_base` (PPA)      | `ppa_tarifas` (año y mes)         | $/kWh  |

`tarifa_mensual` de mantenimiento y arriendo es la anual / 12: no se guarda aparte.
Un servicio sin historial queda solo con su base, sin filas: no se inventa un año.
"""
from __future__ import annotations

from decimal import Decimal

from apps.contratos.services import grupos

#: Servicio → (columna de la base, columna del historial anual o None).
FUENTE = {
    grupos.REPRESENTACION: ("tarifa_representacion", "indexacion_representacion"),
    grupos.CGM: ("tarifa_cgm", "indexacion_cgm"),
    grupos.ADMINISTRACION: ("tarifa_admin", None),
    # Solo la base: el panel de O&M y el de arriendos calculan la indexación de cada
    # año desde ella con las tablas de IPC. Los historiales `indexacion_anual` de
    # estos contratos son una importación de Excel que ningún cálculo usa, y no
    # cuadran (años repetidos con dos valores, bases distintas de `tarifa_base`).
    grupos.MANTENIMIENTO: ("tarifa_base", None),
    grupos.ARRIENDO: ("tarifa_base", None),
    grupos.INTERNET: ("tarifa_mensual", "indexacion_mensual"),
}

#: Los servicios de un PPA: un contrato tiene uno de los dos.
SERVICIOS_PPA = (grupos.COMPRA, grupos.VENTA)

#: Servicio → la unidad de su tarifa.
UNIDAD = {
    grupos.REPRESENTACION: "$/kWh",
    grupos.CGM: "$/kWh",
    grupos.ADMINISTRACION: "%",
    grupos.MANTENIMIENTO: "$/año",
    grupos.ARRIENDO: "$/año",
    grupos.INTERNET: "$/mes",
    grupos.COMPRA: "$/kWh",
    grupos.VENTA: "$/kWh",
}


def tipo_indexacion_de(contrato, servicio: str) -> str | None:
    """El índice con que se actualiza: el que diga `indice_indexacion` (texto libre:
    "IPP serie oferta interna (marzo 2025)"…), IPC si no dice nada. None si no se
    indexa: la administración, o un contrato que dice "Fijo"."""
    from apps.contratos.models import TipoIndexacion

    if servicio == grupos.ADMINISTRACION:
        return None
    texto = (getattr(contrato, "indice_indexacion", None) or "").lower()
    if "ipp" in texto:
        return TipoIndexacion.IPP
    if "fij" in texto:
        return None
    return TipoIndexacion.IPC


def _decimal(valor):
    if valor in (None, ""):
        return None
    return Decimal(str(valor))


def filas_desde_historial(historial) -> list[dict]:
    """Las filas anuales de un historial JSON (`[{año, valor, ipc, esBase}…]`).

    Puro. `[{anio, mes: None, valor}]`, ordenadas por año, sin duplicar años (si se
    repite, gana la última entrada)."""
    filas: dict = {}
    for entrada in historial or []:
        if not isinstance(entrada, dict):
            continue
        anio = entrada.get("año", entrada.get("anio"))
        valor = _decimal(entrada.get("valor"))
        if anio is None or valor is None:
            continue
        filas[int(anio)] = {"anio": int(anio), "mes": None, "valor": valor}
    return [filas[k] for k in sorted(filas)]


def filas_desde_ppa(contrato) -> list[dict]:
    """Las filas mensuales de `ppa_tarifas` del contrato. Las que no tienen precio no
    pasan: un periodo sin valor no es una tarifa."""
    from apps.ppa.models import PpaTarifa

    return [
        {"anio": t.año, "mes": t.mes, "valor": t.tarifa}
        for t in PpaTarifa.objects.filter(contrato_id=contrato.pk, tarifa__isnull=False)
        .order_by("año", "mes")
    ]


def _clave(fila) -> tuple:
    return (fila["anio"], fila["mes"], Decimal(fila["valor"]).normalize())


def _igual(a, b) -> bool:
    if a is None or b is None:
        return a is b
    return Decimal(a).normalize() == Decimal(b).normalize()


def _poner(servicio_obj, base, tipo, filas: list[dict]) -> bool:
    """Deja el servicio con esa base, ese índice y exactamente esas filas. True si
    cambió algo."""
    from apps.contratos.models import Tarifa

    cambio = False
    base = _decimal(base)
    if not _igual(servicio_obj.tarifa_base, base) or servicio_obj.tipo_indexacion != tipo:
        servicio_obj.tarifa_base = base
        servicio_obj.tipo_indexacion = tipo
        servicio_obj.save(update_fields=["tarifa_base", "tipo_indexacion", "updated_at"])
        cambio = True

    actuales = [{"anio": t.anio, "mes": t.mes, "valor": t.valor}
                for t in Tarifa.objects.filter(servicio=servicio_obj)]
    if sorted(map(_clave, actuales), key=str) != sorted(map(_clave, filas), key=str):
        Tarifa.objects.filter(servicio=servicio_obj).delete()
        Tarifa.objects.bulk_create([Tarifa(servicio=servicio_obj, **f) for f in filas])
        cambio = True
    return cambio


def sincronizar(contrato) -> bool:
    """Deriva la tarifa de cada servicio del contrato desde sus columnas (y, en un PPA,
    desde `ppa_tarifas`). True si cambió algo.

    En un contrato de servicio también pone o quita el cargo de administración: lo
    tiene si tiene `tarifa_admin` y lo admite (representación/CGM u O&M,
    `grupos.admite_administracion`)."""
    from apps.contratos.models import GrupoContrato, Servicio

    if not contrato.pk:
        return False
    cambio = False

    if contrato.grupo == GrupoContrato.PPA:
        servicios_ppa = list(Servicio.objects.filter(contrato_id=contrato.pk,
                                                     servicio__in=SERVICIOS_PPA))
        filas = filas_desde_ppa(contrato) if servicios_ppa else []
        for servicio_obj in servicios_ppa:
            cambio |= _poner(servicio_obj, contrato.tarifa_base,
                             tipo_indexacion_de(contrato, servicio_obj.servicio), filas)
    else:
        if grupos.admite_administracion(contrato) and contrato.tarifa_admin is not None:
            _, creado = Servicio.objects.get_or_create(
                contrato_id=contrato.pk, servicio=grupos.ADMINISTRACION)
            cambio |= creado
        else:
            borrados, _ = Servicio.objects.filter(
                contrato_id=contrato.pk, servicio=grupos.ADMINISTRACION).delete()
            cambio |= bool(borrados)

        for servicio_obj in Servicio.objects.filter(contrato_id=contrato.pk):
            if servicio_obj.servicio not in FUENTE:
                continue
            columna_base, columna_historial = FUENTE[servicio_obj.servicio]
            historial = getattr(contrato, columna_historial, None) if columna_historial else None
            cambio |= _poner(
                servicio_obj, getattr(contrato, columna_base, None),
                tipo_indexacion_de(contrato, servicio_obj.servicio),
                filas_desde_historial(historial),
            )
    getattr(contrato, "_prefetched_objects_cache", {}).pop("servicios", None)
    return cambio


def avisos_de_administracion(contrato) -> list[str]:
    """Avisos (no bloquean) si la planta pagaría administración dos veces.

    Decisión de Sara (2026-10-13): avisar y no bloquear --puede haber transiciones
    legítimas, un contrato que termina y otro que empieza--. Se mira entre los
    contratos vivos de la misma planta que también tienen el cargo."""
    from apps.contratos.models import ContratoServicio
    from apps.contratos.services import vigencia
    from apps.plataforma.services.fechas import hoy_col

    if not contrato.pk or not contrato.proyecto_id or not contrato.servicios.filter(
        servicio=grupos.ADMINISTRACION,
    ).exists():
        return []
    otros = (
        ContratoServicio.objects
        .filter(vigencia.filtro_vivos(hoy_col()),
                proyectos_vinculados__proyecto_id=contrato.proyecto_id,
                servicios__servicio=grupos.ADMINISTRACION)
        .exclude(pk=contrato.pk)
        .order_by("id")
    )
    return [
        f"La planta ya paga administración en el contrato "
        f"{otro.numero_contrato or f'#{otro.id}'} ({otro.servicio_aplica}). "
        "Revisa que no se cobre dos veces."
        for otro in otros
    ]
