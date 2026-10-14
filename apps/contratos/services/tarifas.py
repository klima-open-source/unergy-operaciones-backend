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
`ppa_tarifas`. Las filas se llenaron desde ellos con `registrar_tarifas` (paso 1,
borrado en el paso 2; está en el historial de git).

| Servicio        | Atajos del contrato                              | Unidad |
|-----------------|--------------------------------------------------|--------|
| representacion  | `tarifa_representacion`, `indexacion_representacion` | $/kWh |
| cgm             | `tarifa_cgm`, `indexacion_cgm`                   | $/kWh  |
| administracion  | `tarifa_admin` (cargo adicional, % fijo)         | %      |
| mantenimiento   | `tarifa_base` = `tarifa_anual` (+ mensual = /12) | $/año  |
| arriendo        | `tarifa_base` = `tarifa_anual` (+ mensual = /12) | $/año  |
| internet        | `tarifa_mensual`, `indexacion_mensual`           | $/mes  |
| compra / venta  | `tarifa_base`; por mes, `precios_ppa` / `filas_ppa` | $/kWh |
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from apps.contratos.services import grupos

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


def avisos_de_administracion(contrato) -> list[str]:
    """Avisos (no bloquean) si la planta pagaría administración dos veces.

    Decisión de Sara (2026-10-13): avisar y no bloquear --puede haber transiciones
    legítimas, un contrato que termina y otro que empieza--. Se mira entre los
    contratos vivos de la misma planta que también tienen el cargo.

    En una minigranja cada inversionista tiene su contrato de representación y
    paga administración sobre su parte: dos contratos que representan a
    inversionistas DISTINTOS no se cobran dos veces. Sí avisa si representan al
    mismo, o si alguno no representa a nadie en particular (un O&M, que cubre toda
    la planta, o uno de representación sin participación asignada)."""
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
        .values("id", "numero_contrato", "servicio_aplica",
                "proyectos_vinculados__proyecto_inversionista_id")
        .order_by("id")
    )
    mia = (contrato.proyectos_vinculados.values_list("proyecto_inversionista_id", flat=True)
           .first())
    otros = [
        SimpleNamespace(id=o["id"], numero_contrato=o["numero_contrato"],
                        servicio_aplica=o["servicio_aplica"])
        for o in otros
        if mia is None or o["proyectos_vinculados__proyecto_inversionista_id"] in (None, mia)
    ]
    return [
        f"La planta ya paga administración en el contrato "
        f"{otro.numero_contrato or f'#{otro.id}'} ({otro.servicio_aplica}). "
        "Revisa que no se cobre dos veces."
        for otro in otros
    ]


# ── Los atajos de siempre, leídos y escritos sobre el servicio y `tarifas` ─────
#
# `contrato.tarifa_cgm`, `contrato.indexacion_representacion`… se siguen leyendo y
# escribiendo con los nombres de cuando eran columnas (ver `ATAJOS_DE_TARIFA` en
# `models.py`): la API conserva su forma y el front no cambia. Para un listado:
# `.prefetch_related(tarifas.con_tarifas())`.

#: Atajo → (de qué servicio, qué representa). None = el que diga el contrato.
ATAJOS = {
    "tarifa_base": (None, "base_contrato"),
    "tarifa_representacion": (grupos.REPRESENTACION, "base"),
    "tarifa_cgm": (grupos.CGM, "base"),
    "tarifa_admin": (grupos.ADMINISTRACION, "base"),
    "tarifa_anual": (None, "base_anual"),
    "tarifa_mensual": (None, "mensual"),
    "indexacion_representacion": (grupos.REPRESENTACION, "historial"),
    "indexacion_cgm": (grupos.CGM, "historial"),
    "indexacion_anual": (None, "historial_anual"),
    "indexacion_mensual": (None, "historial_mensual"),
}

ANUALES = (grupos.MANTENIMIENTO, grupos.ARRIENDO)
HISTORIALES = ("historial", "historial_anual", "historial_mensual")


def con_tarifas():
    """Para un listado: `Contrato.objects.prefetch_related(tarifas.con_tarifas())` trae
    los servicios y sus tarifas en dos consultas. Reemplaza a `"servicios"` a secas
    (no se pueden pedir los dos: el mismo prefetch con distinto queryset falla)."""
    from django.db.models import Prefetch

    from apps.contratos.models import Servicio, Tarifa

    return Prefetch(
        "servicios",
        queryset=Servicio.objects.prefetch_related(
            Prefetch("tarifas", queryset=Tarifa.objects.order_by("anio", "mes"))
        ),
    )


def _servicio(contrato, servicio):
    for s in contrato.servicios.all():
        if s.servicio == servicio:
            return s
    return None


def _servicio_ppa(contrato):
    for s in contrato.servicios.all():
        if s.servicio in SERVICIOS_PPA:
            return s
    return None


def _base(contrato, servicio):
    s = _servicio(contrato, servicio) if contrato.pk else None
    return s.tarifa_base if s else None


def _anuales(contrato, servicio):
    """Las filas por año (sin mes) del servicio, en orden."""
    s = _servicio(contrato, servicio) if contrato.pk else None
    if s is None:
        return []
    return sorted((t for t in s.tarifas.all() if t.mes is None), key=lambda t: t.anio)


def _variacion(valor, anterior) -> float | None:
    """El índice aplicado de un año al siguiente, en %: sale de comparar los valores
    (ya no se guarda). 6 → 6,312 da 5,2."""
    if anterior in (None, 0):
        return None
    return round((float(valor) / float(anterior) - 1) * 100, 4)


def _historial(filas, divisor=1, claves=("año", "ipc", "valor", "esBase")):
    anio_k, ipc_k, valor_k, base_k = claves
    salida = []
    for i, t in enumerate(filas):
        salida.append({
            anio_k: t.anio,
            ipc_k: None if i == 0 else _variacion(t.valor, filas[i - 1].valor),
            valor_k: round(float(t.valor) / divisor, 2) if divisor != 1 else float(t.valor),
            **({base_k: True} if i == 0 else {}),
        })
    return salida


def leer(contrato, atajo):
    """El valor del atajo, como cuando era columna."""
    from apps.contratos.models import GrupoContrato

    servicio, que = ATAJOS[atajo]
    aplica = getattr(contrato, "servicio_aplica", None)
    if que == "base":
        return _base(contrato, servicio)
    if que == "base_contrato":
        if contrato.grupo == GrupoContrato.PPA:
            s = _servicio_ppa(contrato) if contrato.pk else None
            return s.tarifa_base if s else None
        return _base(contrato, aplica) if aplica in ANUALES else None
    if que == "base_anual":
        return _base(contrato, aplica) if aplica in ANUALES else None
    if que == "mensual":
        if aplica == grupos.INTERNET:
            return _base(contrato, aplica)
        if aplica in ANUALES:
            # El año vigente (el último cargado) / 12; sin años, la base.
            filas = _anuales(contrato, aplica)
            anual = filas[-1].valor if filas else _base(contrato, aplica)
            return round(anual / 12, 2) if anual is not None else None
        return None
    if que == "historial":
        filas = _anuales(contrato, servicio)
        return _historial(filas) if filas else None
    # historial_anual / historial_mensual
    if aplica in ANUALES:
        divisor = 1 if que == "historial_anual" else 12
    elif aplica == grupos.INTERNET and que == "historial_mensual":
        divisor = 1
    else:
        return None
    filas = _anuales(contrato, aplica)
    if not filas:
        return None
    return _historial(filas, divisor, claves=("anio", "ipc_aplicado", "valor", "es_base"))


def _servicio_destino(contrato, atajo):
    """El servicio al que va lo escrito en el atajo, o None si en este contrato no aplica
    (p. ej. `tarifa_mensual` de un mantenimiento: sale de la anual)."""
    from apps.contratos.models import GrupoContrato

    servicio, que = ATAJOS[atajo]
    aplica = getattr(contrato, "servicio_aplica", None)
    if que == "base_contrato":
        if contrato.grupo == GrupoContrato.PPA:
            return SERVICIOS_PPA
        return aplica if aplica in ANUALES else None
    if que in ("base_anual", "historial_anual"):
        return aplica if aplica in ANUALES else None
    if que in ("mensual", "historial_mensual"):
        return aplica if aplica == grupos.INTERNET else None
    return servicio


def _escribir_base(servicio_obj, valor) -> None:
    servicio_obj.tarifa_base = _decimal(valor)
    servicio_obj.save(update_fields=["tarifa_base", "updated_at"])


def _escribir_historial(servicio_obj, historial) -> None:
    """Reemplaza las filas por año (sin mes) del servicio por las del historial."""
    from apps.contratos.models import Tarifa

    nuevas = {}
    for entrada in historial or []:
        if not isinstance(entrada, dict):
            continue
        anio = entrada.get("año", entrada.get("anio"))
        valor = _decimal(entrada.get("valor"))
        if anio is None or valor is None:
            continue
        nuevas[int(anio)] = Tarifa(servicio=servicio_obj, anio=int(anio), valor=valor)
    Tarifa.objects.filter(servicio=servicio_obj, mes__isnull=True).delete()
    Tarifa.objects.bulk_create(nuevas.values())


def _alinear_indices(contrato) -> None:
    """El índice de cada servicio sigue al `indice_indexacion` del contrato."""
    from apps.contratos.models import Servicio

    for s in Servicio.objects.filter(contrato_id=contrato.pk):
        tipo = tipo_indexacion_de(contrato, s.servicio)
        if s.tipo_indexacion != tipo:
            s.tipo_indexacion = tipo
            s.save(update_fields=["tipo_indexacion", "updated_at"])


def aplicar(contrato) -> None:
    """Lleva al servicio y a `tarifas` lo asignado a los atajos del contrato, y alinea
    el índice de sus servicios con `indice_indexacion`.

    Lo que todavía no tiene su servicio (un CGM que llega con la lista de servicios
    después del `save()`) queda pendiente para la siguiente llamada; la API de
    contratos la hace después de registrar la lista."""
    from apps.contratos.models import Servicio

    if not contrato.pk:
        return
    pendientes = contrato.__dict__.get("_tarifas_pendientes") or {}
    sin_servicio = {}
    # Primero los historiales, después las bases.
    for atajo in sorted(pendientes, key=lambda a: ATAJOS[a][1] not in HISTORIALES):
        valor = pendientes[atajo]
        servicio = _servicio_destino(contrato, atajo)
        if servicio is None:
            continue
        if servicio == grupos.ADMINISTRACION:
            if valor in (None, "") or not grupos.admite_administracion(contrato):
                Servicio.objects.filter(contrato_id=contrato.pk, servicio=servicio).delete()
                continue
            servicio_obj, _ = Servicio.objects.get_or_create(
                contrato_id=contrato.pk, servicio=servicio)
        else:
            servicios = servicio if isinstance(servicio, tuple) else (servicio,)
            servicio_obj = Servicio.objects.filter(
                contrato_id=contrato.pk, servicio__in=servicios).first()
            if servicio_obj is None:
                if valor not in (None, [], ""):
                    sin_servicio[atajo] = valor
                continue
        if ATAJOS[atajo][1] in HISTORIALES:
            _escribir_historial(servicio_obj, valor)
        else:
            _escribir_base(servicio_obj, valor)
    contrato.__dict__["_tarifas_pendientes"] = sin_servicio
    _alinear_indices(contrato)
    getattr(contrato, "_prefetched_objects_cache", {}).pop("servicios", None)


# ── Los precios mes a mes de un PPA (antes `ppa_tarifas`) ────────────────────

def filas_ppa(contrato) -> list:
    """Las tarifas por mes del PPA, en orden: `[Tarifa(anio, mes, valor)…]`. Usa los
    servicios precargados (`con_tarifas()`) si los hay."""
    if not contrato.pk:
        return []
    filas = [t for s in contrato.servicios.all() if s.servicio in SERVICIOS_PPA
             for t in s.tarifas.all() if t.mes is not None]
    return sorted(filas, key=lambda t: (t.anio, t.mes))


def precios_ppa(anio: int, mes: int, contrato_ids=None) -> dict[int, Decimal]:
    """{contrato_id: precio} de los PPA con precio en ese mes. Lo que antes era
    `PpaTarifa.objects.filter(año=…, mes=…)`."""
    from apps.contratos.models import Tarifa

    filas = Tarifa.objects.filter(servicio__servicio__in=SERVICIOS_PPA, anio=anio, mes=mes)
    if contrato_ids is not None:
        filas = filas.filter(servicio__contrato_id__in=contrato_ids)
    return dict(filas.values_list("servicio__contrato_id", "valor"))


def poner_filas_ppa(contrato, filas: list[dict]) -> None:
    """Reemplaza TODOS los precios por mes del PPA por `[{año, mes, tarifa}…]`.

    Las filas sin precio no se guardan: un periodo sin valor no es una tarifa. Si el
    contrato no tiene servicio de compra ni de venta, no hay dónde ponerlas: error."""
    from apps.contratos.models import Servicio, Tarifa

    servicio_obj = Servicio.objects.filter(contrato_id=contrato.pk,
                                           servicio__in=SERVICIOS_PPA).first()
    if servicio_obj is None:
        raise ValueError(f"El PPA {contrato.pk} no tiene servicio de compra ni de venta.")
    nuevas = {}
    for fila in filas or []:
        anio = fila.get("año", fila.get("anio"))
        valor = _decimal(fila.get("tarifa", fila.get("valor")))
        if anio is None or fila.get("mes") is None or valor is None:
            continue
        nuevas[(int(anio), int(fila["mes"]))] = Tarifa(
            servicio=servicio_obj, anio=int(anio), mes=int(fila["mes"]), valor=valor)
    Tarifa.objects.filter(servicio=servicio_obj, mes__isnull=False).delete()
    Tarifa.objects.bulk_create(nuevas.values())
    getattr(contrato, "_prefetched_objects_cache", {}).pop("servicios", None)
