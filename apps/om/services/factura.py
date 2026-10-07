"""La factura consolidada mensual del proveedor de O&M y su división por proyecto.

El proveedor manda UN PDF con la factura de todas las plantas del mes. Se
guarda, se parte por proyecto (`pdf_splitter`) y lo que no se pueda emparejar
queda como página «sin match» para asignarla a mano.
"""

from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.contratos import models as ct_models
from apps.contratos.services import contrato_partes
from apps.om import models as om_models
from apps.om.services.calculadora import calcular_proyecto  # noqa: F401
from apps.om.services.pdf_splitter import (
    dividir_pdf, escribir_o_anexar_pagina, extraer_pagina_datos, safe_filename,
)

SERVICIO_OM = "mantenimiento"

# Campos que el splitter extrae del PDF y que se copian al documento.
CAMPOS_EXTRAIDOS = (
    "numero_factura", "total_sin_impuestos", "iva", "total_pagar",
    "fecha_facturacion", "cufe",
)


class SinPdfOriginal(LookupError):
    pass


class YaAsignada(ValueError):
    pass


def directorio() -> Path:
    return Path(settings.BASE_DIR) / "uploads" / "om"


def directorio_documentos(periodo: str) -> Path:
    return directorio() / "documentos" / periodo


def nombre_proyecto_de(contrato) -> str:
    proyecto = contrato.proyecto
    return (
        (proyecto.nombre_comercial if proyecto else None)
        or contrato.prestador_nombre
        or f"Contrato #{contrato.id}"
    )


def contratos_para_split() -> list[dict]:
    """TODOS los contratos de mantenimiento, sin filtrar por estado del proyecto.

    Una factura puede incluir proyectos en cualquier estado; el filtro por
    `en_operacion` es solo del panel.
    """
    return [
        {"contrato_id": c.id, "nombre_proyecto": nombre_proyecto_de(c)}
        for c in ct_models.ContratoServicio.objects
        .filter(servicio_aplica=SERVICIO_OM)
        .select_related("proyecto").prefetch_related(contrato_partes.CON_PARTES)
        .order_by("id")
    ]


def guardar_pdf(periodo: str, archivo) -> tuple[Path, object]:
    """Escribe el PDF en disco y hace upsert del registro de la factura."""
    carpeta = directorio()
    carpeta.mkdir(parents=True, exist_ok=True)

    extension = Path(archivo.name or "factura.pdf").suffix or ".pdf"
    ruta = carpeta / f"{periodo}{extension}"
    ruta.write_bytes(archivo.read())

    factura, _ = om_models.OmFacturaMensual.objects.update_or_create(
        periodo=periodo,
        defaults={
            "nombre_archivo": archivo.name,
            "ruta_local": str(ruta),
            # Subir un archivo invalida el enlace externo que hubiera.
            "enlace_pdf": None,
        },
    )
    return ruta, factura


@transaction.atomic
def aplicar_split(periodo: str, resultado: dict) -> None:
    """Guarda documentos, borra huérfanos y reemplaza las páginas sin match."""
    for item in resultado["procesados"]:
        om_models.OmDocumentoProyecto.objects.update_or_create(
            contrato_id=item["contrato_id"], periodo=periodo,
            defaults={
                "nombre_archivo": item["archivo"],
                "ruta_local": item["ruta_local"],
                **{c: item.get(c) for c in CAMPOS_EXTRAIDOS},
            },
        )

    # Documentos huérfanos: contratos que tenían documento este período y ya no
    # aparecen en el split nuevo. Solo si el split procesó ALGO — con cero
    # procesados no se borra nada, para no perder datos por una subida mala.
    procesados = {item["contrato_id"] for item in resultado["procesados"]}
    if procesados:
        om_models.OmDocumentoProyecto.objects.filter(
            periodo=periodo
        ).exclude(contrato_id__in=procesados).delete()

    # Una resubida invalida la numeración de página de los sin_match anteriores,
    # así que se reemplazan por los del split actual.
    om_models.OmPaginaSinMatch.objects.filter(periodo=periodo).delete()
    om_models.OmPaginaSinMatch.objects.bulk_create([
        om_models.OmPaginaSinMatch(
            periodo=periodo,
            pagina=item["pagina"],
            nombre_extraido=item.get("nombre_extraido"),
            estrategia=item.get("estrategia"),
            razon=item["razon"],
            numero_factura=item.get("numero_factura"),
            muestra_texto=item.get("muestra_texto"),
            origen="upload",
        )
        for item in resultado["sin_match"]
    ])


def subir(periodo: str, archivo) -> dict:
    """Guarda el PDF y lo divide. Si el split falla, la factura SÍ queda guardada."""
    ruta, _factura = guardar_pdf(periodo, archivo)
    try:
        resultado = dividir_pdf(
            ruta, periodo, contratos_para_split(), directorio_documentos(periodo)
        )
    except Exception as exc:
        return {
            "ok": True, "nombre_archivo": archivo.name, "periodo": periodo,
            "splitting_result": {
                "procesados": 0, "sin_match": [], "detalle": [],
                "error": str(exc),
            },
        }

    aplicar_split(periodo, resultado)
    return {
        "ok": True, "nombre_archivo": archivo.name, "periodo": periodo,
        "splitting_result": {
            "procesados": len(resultado["procesados"]),
            "sin_match": resultado["sin_match"],
            "detalle": resultado["procesados"],
        },
    }


@transaction.atomic
def asignar_sin_match(sin_match, contrato) -> object:
    """Extrae la página del PDF original y la anexa al documento del contrato.

    Anexa en vez de reemplazar: una planta puede tener varias páginas en la
    misma factura, y la asignación manual llega de una en una.
    """
    if sin_match.resuelto:
        raise YaAsignada("Esta página ya fue asignada")

    factura = om_models.OmFacturaMensual.objects.filter(
        periodo=sin_match.periodo
    ).first()
    if (
        factura is None or not factura.ruta_local
        or not Path(factura.ruta_local).exists()
    ):
        raise SinPdfOriginal(
            "No hay PDF consolidado original disponible para este período"
        )

    origen = Path(factura.ruta_local)
    datos = extraer_pagina_datos(origen, sin_match.pagina)

    nombre = nombre_proyecto_de(contrato)
    archivo = (
        f"SOFV_{safe_filename(nombre)}_{sin_match.periodo}_mantenimiento.pdf"
    )
    destino = directorio_documentos(sin_match.periodo) / archivo
    escribir_o_anexar_pagina(origen, sin_match.pagina, destino)

    documento = om_models.OmDocumentoProyecto.objects.filter(
        contrato=contrato, periodo=sin_match.periodo
    ).first()
    if documento is None:
        documento = om_models.OmDocumentoProyecto(
            contrato=contrato, periodo=sin_match.periodo
        )
    documento.nombre_archivo = archivo
    documento.ruta_local = str(destino)
    for campo in CAMPOS_EXTRAIDOS:
        # `or` y no asignación directa: la página nueva puede no traer un dato
        # que la anterior sí tenía, y perderlo sería un retroceso.
        setattr(
            documento, campo,
            datos.get(campo) or getattr(documento, campo, None),
        )
    documento.save()

    sin_match.resuelto = True
    sin_match.contrato_id_asignado = contrato.id
    sin_match.asignado_en = timezone.now()
    sin_match.save(
        update_fields=["resuelto", "contrato_id_asignado", "asignado_en"]
    )
    return documento


def info(periodo: str) -> dict:
    """Estado de la factura del período y sus páginas sin resolver."""
    factura = om_models.OmFacturaMensual.objects.filter(periodo=periodo).first()
    if factura is None:
        return {
            "periodo": periodo, "nombre_archivo": None, "enlace_pdf": None,
            "tiene_archivo": False, "subido_en": None,
            "sin_match_pendientes": [],
        }
    pendientes = om_models.OmPaginaSinMatch.objects.filter(
        periodo=periodo, resuelto=False
    ).order_by("pagina")
    return {
        "periodo": periodo,
        "nombre_archivo": factura.nombre_archivo,
        "enlace_pdf": factura.enlace_pdf,
        "tiene_archivo": bool(
            factura.ruta_local and Path(factura.ruta_local).exists()
        ),
        "subido_en": factura.subido_en,
        "sin_match_pendientes": [
            {
                "id": p.id, "pagina": p.pagina,
                "nombre_extraido": p.nombre_extraido, "razon": p.razon,
                "numero_factura": p.numero_factura, "origen": p.origen,
            }
            for p in pendientes
        ],
    }


def guardar_enlace(periodo: str, enlace: str | None, nombre: str | None) -> None:
    """Un link externo (Drive…) como factura del período. Borra la ruta local."""
    om_models.OmFacturaMensual.objects.update_or_create(
        periodo=periodo,
        defaults={
            "enlace_pdf": enlace,
            "nombre_archivo": nombre or enlace,
            "ruta_local": None,
        },
    )
