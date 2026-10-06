"""Consultas de contratos PPA."""

from datetime import date

from django.db.models import Q

from apps.ppa import models as ppa_models
from apps.ppa.services import contratos as contratos_service

# Cuántos contratos como máximo devuelve el listado. Es el tope del contrato
# actual: la pantalla no pagina.
LIMITE_MAXIMO = 500


def con_relaciones():
    """Base del listado y del detalle.

    Los `prefetch_related` no son opcionales: `carpeta_link` es una propiedad
    que recorre los documentos comerciales en CADA fila serializada, y sin
    precargarlos un listado de 500 contratos dispara 500 consultas.
    """
    return (
        ppa_models.PpaContrato.objects
        .filter(deleted_at__isnull=True)
        .select_related("responsable", "comprador", "vendedor")
        .prefetch_related(
            "proyectos_vinculados__proyecto",
            "tarifas",
            "compromisos",
            "documentos_comerciales",
        )
    )


def listar(proyecto_id=None, q=None, tipo_contrato=None, limite=LIMITE_MAXIMO):
    consulta = con_relaciones()
    if tipo_contrato is not None:
        consulta = consulta.filter(tipo_contrato=tipo_contrato)
    if proyecto_id is not None:
        consulta = consulta.filter(
            proyectos_vinculados__proyecto_id=proyecto_id
        )
    if q:
        # La búsqueda cruza el nombre del PROYECTO además de los tres campos del
        # contrato: el usuario busca por planta, no por número de contrato.
        consulta = consulta.filter(
            Q(proyectos_vinculados__proyecto__nombre_comercial__icontains=q)
            | Q(nombre_interno__icontains=q)
            | Q(numero_codigo_contrato__icontains=q)
            | Q(comprador_nombre__icontains=q)
        ).distinct()

    return consulta.order_by("-fecha_inicio", "-id")[:limite]


def partes() -> dict:
    """Compradores y vendedores distintos que aparecen en los contratos.

    Sale de los contratos y no de `clientes` porque hay partes que nunca se
    dieron de alta como cliente: el nombre y el NIT están escritos en el PPA.
    """
    def unicos(campo_nombre: str, campo_nit: str) -> list[dict]:
        filas = (
            ppa_models.PpaContrato.objects
            .filter(**{f"{campo_nombre}__isnull": False})
            .values_list(campo_nombre, campo_nit).distinct()
        )
        return [{"nombre": nombre, "nit": nit} for nombre, nit in filas]

    return {
        "compradores": unicos("comprador_nombre", "comprador_nit"),
        "vendedores": unicos("vendedor_nombre", "vendedor_nit"),
    }


def build_resumen_global(hoy: date | None = None) -> dict:
    """Resumen de cartera con las métricas de visibilidad agregadas."""
    hoy = hoy or date.today()
    contratos = list(con_relaciones())

    conteo = {"on_track": 0, "at_risk": 0, "deficit": 0, "sin_datos": 0}
    filas = []
    for contrato in contratos:
        visible = contratos_service.visibilidad(contrato, hoy)
        estado = visible["estado_cumplimiento"]
        conteo[estado if estado else "sin_datos"] += 1
        filas.append({
            "id": contrato.id,
            "nombre_interno": contrato.nombre_interno,
            "numero_codigo_contrato": contrato.numero_codigo_contrato,
            "comprador_nombre": contrato.comprador_nombre,
            "tipo_contrato": contrato.tipo_contrato,
            "fecha_inicio": (
                contrato.fecha_inicio.isoformat()
                if contrato.fecha_inicio else None
            ),
            "fecha_fin": (
                contrato.fecha_fin.isoformat() if contrato.fecha_fin else None
            ),
            **visible,
        })

    return {
        "total_contratos": len(contratos),
        "vigentes": sum(
            1 for c in contratos if c.fecha_fin and c.fecha_fin >= hoy
        ),
        "vencidos": sum(
            1 for c in contratos if c.fecha_fin and c.fecha_fin < hoy
        ),
        "sin_fecha_fin": sum(1 for c in contratos if not c.fecha_fin),
        "cumplimiento": conteo,
        "contratos": filas,
    }
