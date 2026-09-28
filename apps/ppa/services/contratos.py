"""Reglas de los contratos PPA.

Las validaciones cruzadas con GESCON viven acá y no en la vista porque son la
otra mitad de una regla que también aplica `apps/mercado_xm/services/
asic_reglas.py::validar_fecha_fin_vs_ppa`: las dos tienen que decir lo mismo.
"""

from datetime import date

from django.db.models import Q

from apps.clientes import models as cl_models
from apps.mercado_xm import models as mx_models
from apps.ppa import models as ppa_models


class ReglaPpa(ValueError):
    """422 — los datos rompen una regla de negocio."""


class Bloqueado(RuntimeError):
    """409 — hay datos que dependen de este contrato."""


def sincronizar_partes(contrato) -> None:
    """Copia nombre y NIT desde el cliente cuando hay comprador o vendedor.

    Se DUPLICAN a propósito en el contrato: un PPA firmado con una razón social
    tiene que seguir mostrando esa, aunque el cliente se renombre después.
    """
    campos = []
    for rol in ("comprador", "vendedor"):
        cliente_id = getattr(contrato, f"{rol}_id")
        if not cliente_id:
            continue
        cliente = cl_models.Cliente.objects.filter(pk=cliente_id).first()
        if cliente is None:
            continue
        setattr(contrato, f"{rol}_nombre", cliente.razon_social_nombre)
        setattr(contrato, f"{rol}_nit", cliente.nit_cedula)
        campos += [f"{rol}_nombre", f"{rol}_nit"]
    if campos:
        contrato.save(update_fields=campos)


def validar_fecha_fin_vs_asic(contrato) -> None:
    """La fecha de fin del PPA macro no puede quedar ANTES que la de sus plantas.

    Es manual, pero un registro GESCON que termina después dejaría a esa planta
    «vigente» más allá del contrato comercial. Ver la regla inversa en
    `asic_reglas.validar_fecha_fin_vs_ppa`.
    """
    if contrato.fecha_fin is None:
        return

    criterio = Q(contrato_ppa_id=contrato.id)
    if contrato.numero_codigo_contrato:
        criterio |= Q(contrato_interno=contrato.numero_codigo_contrato)

    peor = (
        mx_models.AsicSolicitud.objects
        .filter(criterio, fecha_fin__gt=contrato.fecha_fin)
        .order_by("-fecha_fin").first()
    )
    if peor is not None:
        raise ReglaPpa(
            f"No se puede fijar la fecha de fin en "
            f'{contrato.fecha_fin.isoformat()}: el registro GESCON '
            f'"{peor.codigo_sic_contrato or peor.id}" ya tiene fecha_fin '
            f"{peor.fecha_fin.isoformat()}, posterior. Corrige ese registro "
            f"primero o usa una fecha de fin mayor."
        )


def razones_para_no_borrar(contrato) -> list[str]:
    """Un contrato del que cuelgan liquidaciones o registros GESCON no se borra."""
    razones = []
    cumplimientos = mx_models.CumplimientoMensual.objects.filter(
        contrato_ppa=contrato
    ).count()
    if cumplimientos:
        razones.append(
            f"Tiene {cumplimientos} liquidación(es) de cumplimiento asociadas"
        )
    registros = mx_models.AsicSolicitud.objects.filter(
        contrato_ppa=contrato
    ).count()
    if registros:
        razones.append(f"Tiene {registros} registro(s) GESCON/ASIC vinculados")
    return razones


def fijar_proyectos(contrato, proyecto_ids: list[int]) -> None:
    """Reemplaza el conjunto de proyectos del contrato."""
    ppa_models.PpaContratoProyecto.objects.filter(contrato=contrato).delete()
    if not proyecto_ids:
        return
    ppa_models.PpaContratoProyecto.objects.bulk_create([
        ppa_models.PpaContratoProyecto(contrato=contrato, proyecto_id=pid)
        for pid in proyecto_ids
    ])


# ---------------------------------------------------------------------------
# Visibilidad: qué tan bien va el contrato este mes
# ---------------------------------------------------------------------------

UMBRAL_EN_RIESGO = 80


def visibilidad(contrato, hoy: date | None = None) -> dict:
    """`estado_cumplimiento`, `dias_restantes` y `cobertura_actual_pct`.

    La cobertura es generación del mes contra el compromiso mínimo. Sin
    compromiso cargado o sin generación no hay porcentaje: se devuelve `None` en
    vez de cero, porque «no sabemos» y «no generó» no son lo mismo.
    """
    hoy = hoy or date.today()
    dias = (contrato.fecha_fin - hoy).days if contrato.fecha_fin else None

    compromiso = ppa_models.PpaCompromisoEnergia.objects.filter(
        contrato=contrato, **{"año": hoy.year, "mes": hoy.month}
    ).first()
    minimo = (
        float(compromiso.energia_minima)
        if compromiso and compromiso.energia_minima is not None else None
    )

    cumplimiento = mx_models.CumplimientoMensual.objects.filter(
        contrato_ppa=contrato, anio=hoy.year, mes=hoy.month
    ).first()
    generado = (
        float(cumplimiento.gen_total_mwh)
        if cumplimiento and cumplimiento.gen_total_mwh is not None else None
    )

    cobertura = None
    if minimo and minimo > 0 and generado is not None:
        cobertura = round(generado / minimo * 100, 1)

    if cobertura is not None:
        if cobertura >= 100:
            estado = "on_track"
        elif cobertura >= UMBRAL_EN_RIESGO:
            estado = "at_risk"
        else:
            estado = "deficit"
    elif contrato.fecha_fin and contrato.fecha_fin < hoy:
        # Sin datos pero ya vencido: no es «sin información», es déficit.
        estado = "deficit"
    else:
        estado = None

    return {
        "estado_cumplimiento": estado,
        "dias_restantes": dias,
        "cobertura_actual_pct": cobertura,
    }
