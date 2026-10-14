"""Cuentas de cobro de arriendos traídas de originabot (TSF).

En TSF cada propietario sube cada mes su cuenta de cobro (o factura) para que se
le pague: es un `payment.Payment` del terreno, con su fecha, su estado y lo que
le corresponde de cada proyecto (`rent[]`). originabot los lista por terreno y
año (`ClienteOriginabot.pagos_de_terreno`).

«Traer de TSF», en la pestaña Arriendos de Costos, hace dos pasos:

1. `consultar(periodo)`: las cuentas de ese mes de cada planta con contrato de
   arriendo, con su valor y si ya tienen documento. No escribe nada salvo el
   proyecto y el terreno de TSF de cada planta, que se guardan la primera vez.
2. `importar(periodo, pagos)`: descarga el documento de los pagos elegidos y lo
   guarda como `ArrDocumento` (uno por pago Y planta, con `valor_individual` =
   lo de esa planta), el mismo lugar donde deja los suyos la carga por ZIP.
   Idempotente: volver a importar el mismo pago reemplaza su archivo.

Un pago sin documento todavía (`wait_for_document`) se muestra pero no se importa.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from django.db import transaction

from apps.arriendos import models as ar_models
from apps.arriendos.services import documentos as documentos_service
from apps.comun.integraciones.originabot import ClienteOriginabot, OriginabotError
from apps.comun.nombre_matching import normalizar
from apps.contratos import models as ct_models
from apps.contratos.services import plantas as plantas_service
from apps.plataforma.services.fechas import hoy_col
from apps.proyectos.models import Proyecto

logger = logging.getLogger(__name__)

HILOS = 8


@dataclass
class CuentaTsf:
    pago_id: str
    fecha: str
    estado: str
    estado_texto: str
    proyecto_id: int
    proyecto_nombre: str
    arrendador_id: int | None
    landlord_id: int | None
    landlord_nombre: str
    #: Lo de ESTA planta en el pago (sus líneas de `rent`).
    valor: Decimal
    #: El total a pagar del pago completo (todas sus plantas, con IVA y retenciones).
    total_pago: Decimal | None
    documento_url: str | None
    tipo_documento: str | None
    importado: bool

    def como_dict(self) -> dict:
        d = asdict(self)
        d["valor"] = float(self.valor)
        d["total_pago"] = float(self.total_pago) if self.total_pago is not None else None
        # La URL firmada vence en una hora y no le sirve a la pantalla.
        d["tiene_documento"] = d.pop("documento_url") is not None
        return d


def _decimal(v) -> Decimal | None:
    try:
        return Decimal(str(v)) if v is not None else None
    except ArithmeticError:
        return None


def _del_panel():
    """Las plantas de la tabla de Arriendos (`panel.calculo`): las que operamos y
    tienen contrato de arriendo. Traer cuentas de otras las dejaría sin dónde verse."""
    con_arriendo = ct_models.ContratoServicio.objects.filter(
        servicio_aplica="arriendo", proyecto__isnull=False, deleted_at__isnull=True,
    ).values_list("proyecto_id", flat=True)
    return Proyecto.objects.filter(
        plantas_service.filtro_operadas(hoy_col()), id__in=con_arriendo, deleted_at__isnull=True,
    )


def _plantas() -> list[Proyecto]:
    """Las plantas de la tabla de Arriendos con minigranja de TSF."""
    return list(_del_panel().filter(originabot_minifarm_id__isnull=False).order_by("nombre_comercial"))


def _completar_ids_tsf(plantas: list[Proyecto], ob: ClienteOriginabot) -> list[str]:
    """Guarda el proyecto y el terreno de TSF de las plantas que no los tienen.

    Devuelve los errores (una línea por planta) sin cortar: una minigranja que
    originabot no encuentre no impide traer las demás.
    """
    faltan = [p for p in plantas if not (p.originabot_proyecto_id and p.originabot_terreno_id)]
    if not faltan:
        return []

    def leer(planta):
        try:
            return planta, ob.minigranja(planta.originabot_minifarm_id), None
        except OriginabotError as exc:
            return planta, None, str(exc)

    errores = []
    with ThreadPoolExecutor(HILOS) as pool:
        for planta, minigranja, error in pool.map(leer, faltan):
            proyecto = (minigranja or {}).get("project") or {}
            pid, tid = proyecto.get("id"), proyecto.get("terrain_id")
            if not (isinstance(pid, int) and isinstance(tid, int)):
                errores.append(f"{planta.nombre_comercial}: {error or 'TSF no da su terreno.'}")
                continue
            Proyecto.objects.filter(pk=planta.pk).update(
                originabot_proyecto_id=pid, originabot_terreno_id=tid,
            )
            planta.originabot_proyecto_id, planta.originabot_terreno_id = pid, tid
    return errores


def _arrendador(planta: Proyecto, landlord: dict) -> ar_models.ArrArrendador | None:
    """El arrendador del contrato de arriendo de la planta que es este propietario."""
    del_contrato = ar_models.ArrArrendador.objects.filter(
        contrato__proyecto_id=planta.id, contrato__servicio_aplica="arriendo",
    )
    por_llave = del_contrato.filter(originabot_landlord_id=landlord.get("id")).first()
    if por_llave:
        return por_llave
    clave = normalizar(landlord.get("name") or "")
    por_nombre = [a for a in del_contrato if clave and normalizar(a.nombre) == clave]
    return por_nombre[0] if len(por_nombre) == 1 else None


def _tipo_documento(url: str | None) -> str | None:
    if not url:
        return None
    return "factura_electronica" if "electronic_invoice" in url else "cuenta_cobro"


def _consultar(periodo: str, ob: ClienteOriginabot) -> tuple[list[CuentaTsf], list[str], list[str]]:
    anio = int(periodo[:4])
    plantas = _plantas()
    errores = _completar_ids_tsf(plantas, ob)
    con_ids = [p for p in plantas if p.originabot_proyecto_id and p.originabot_terreno_id]

    por_terreno: dict[int, list[Proyecto]] = {}
    for p in con_ids:
        por_terreno.setdefault(p.originabot_terreno_id, []).append(p)

    def leer(terreno):
        try:
            return terreno, ob.pagos_de_terreno(terreno, anio), None
        except OriginabotError as exc:
            return terreno, [], str(exc)

    importados = set(
        ar_models.ArrDocumento.objects.filter(
            periodo=periodo, originabot_pago_id__isnull=False,
        ).values_list("originabot_pago_id", "proyecto_id")
    )

    cuentas: list[CuentaTsf] = []
    with ThreadPoolExecutor(HILOS) as pool:
        for terreno, pagos, error in pool.map(leer, por_terreno):
            if error:
                nombres = ", ".join(p.nombre_comercial for p in por_terreno[terreno])
                errores.append(f"Terreno {terreno} ({nombres}): {error}")
            for pago in pagos:
                # La fecha del pago es un día (`2026-10-20T00:00:00Z`): su mes es el período.
                if str(pago.get("date") or "")[:7] != periodo:
                    continue
                landlord = pago.get("land_lord") or {}
                documento = (pago.get("support_document") or {}).get("file")
                estado = pago.get("status") or {}
                for planta in por_terreno[terreno]:
                    lineas = [
                        r for r in pago.get("rent") or []
                        if (r.get("project") or {}).get("id") == planta.originabot_proyecto_id
                    ]
                    if not lineas:
                        continue
                    arrendador = _arrendador(planta, landlord)
                    cuentas.append(CuentaTsf(
                        pago_id=str(pago["id"]),
                        fecha=str(pago.get("date") or "")[:10],
                        estado=str(estado.get("value") or ""),
                        estado_texto=str(estado.get("display") or estado.get("value") or ""),
                        proyecto_id=planta.id,
                        proyecto_nombre=planta.nombre_comercial or "",
                        arrendador_id=arrendador.id if arrendador else None,
                        landlord_id=landlord.get("id"),
                        landlord_nombre=str(landlord.get("name") or ""),
                        valor=sum((_decimal(r.get("amount")) or Decimal(0) for r in lineas), Decimal(0)),
                        total_pago=_decimal((pago.get("summary") or {}).get("total_to_pay")),
                        documento_url=documento or None,
                        tipo_documento=_tipo_documento(documento),
                        importado=(str(pago["id"]), planta.id) in importados,
                    ))

    sin_minigranja = list(
        _del_panel().filter(originabot_minifarm_id__isnull=True)
        .order_by("nombre_comercial").values_list("nombre_comercial", flat=True)
    )
    cuentas.sort(key=lambda c: (c.proyecto_nombre, c.fecha, c.landlord_nombre))
    return cuentas, errores, sin_minigranja


def consultar(periodo: str, token: str) -> dict:
    """Las cuentas de cobro de TSF del período, por planta. Ver el docstring del módulo."""
    cuentas, errores, sin_minigranja = _consultar(periodo, ClienteOriginabot(token))
    return {
        "periodo": periodo,
        "cuentas": [c.como_dict() for c in cuentas],
        "errores": errores,
        "sin_minigranja": sin_minigranja,
    }


def _nombre_archivo(cuenta: CuentaTsf, planta: Proyecto, url: str) -> str:
    original = PurePosixPath(unquote(urlparse(url).path)).name
    ext = PurePosixPath(original).suffix or ".pdf"
    partes = [
        planta.codigo_tsf or planta.origina_code or f"proyecto-{planta.id}",
        cuenta.fecha,
        cuenta.landlord_nombre or "arrendador",
        planta.nombre_comercial or "",
        cuenta.pago_id[:8],
    ]
    return documentos_service.nombre_seguro("_".join(p for p in partes if p) + ext, f"pago_{cuenta.pago_id}{ext}")


def importar(periodo: str, token: str, pago_ids: list[str]) -> dict:
    """Descarga y guarda el documento de los pagos elegidos. Ver el docstring del módulo.

    Se vuelve a consultar TSF aquí y no se reciben URLs de la pantalla: las
    firmadas vencen en una hora, y así solo se descarga lo que TSF dice que es
    de ese período y esas plantas.
    """
    ob = ClienteOriginabot(token)
    elegidos = set(pago_ids)
    cuentas, errores, _ = _consultar(periodo, ob)
    a_importar = [c for c in cuentas if c.pago_id in elegidos and c.documento_url]
    plantas = {p.id: p for p in Proyecto.objects.filter(id__in={c.proyecto_id for c in a_importar})}

    # Un pago puede cubrir varias plantas: su archivo se descarga una sola vez.
    def bajar(url):
        try:
            return url, ob.descargar(url), None
        except OriginabotError as exc:
            return url, None, str(exc)

    archivos: dict[str, bytes] = {}
    with ThreadPoolExecutor(HILOS) as pool:
        for url, contenido, error in pool.map(bajar, {c.documento_url for c in a_importar}):
            if contenido is None:
                errores.append(error)
            else:
                archivos[url] = contenido

    guardados = 0
    for cuenta in a_importar:
        contenido = archivos.get(cuenta.documento_url)
        if contenido is None:
            continue
        planta = plantas[cuenta.proyecto_id]
        codigo = (planta.codigo_tsf or planta.origina_code or f"proyecto-{planta.id}")[:120]
        nombre = _nombre_archivo(cuenta, planta, cuenta.documento_url)
        ruta = documentos_service.escribir(documentos_service.carpeta(periodo, codigo), nombre, contenido)
        with transaction.atomic():
            documento = ar_models.ArrDocumento.objects.filter(
                originabot_pago_id=cuenta.pago_id, proyecto_id=planta.id,
            ).first() or ar_models.ArrDocumento(
                originabot_pago_id=cuenta.pago_id, proyecto_id=planta.id, pago_id=0,
            )
            documento.periodo = periodo
            documento.arr_arrendador_id = cuenta.arrendador_id
            documento.codigo_contrato = codigo
            documento.tipo_documento = cuenta.tipo_documento or "cuenta_cobro"
            documento.nombre_archivo = nombre
            documento.ruta_local = str(ruta)
            documento.codigo_predio = planta.codigo_tsf
            documento.nombre_arrendatario = cuenta.landlord_nombre
            documento.valor_individual = cuenta.valor
            documento.save()
        guardados += 1

    return {
        "importados": guardados,
        "sin_documento": len([c for c in cuentas if c.pago_id in elegidos and not c.documento_url]),
        "errores": errores,
    }
