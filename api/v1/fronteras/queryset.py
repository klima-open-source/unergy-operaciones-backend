"""Consultas del catálogo de fronteras."""

from django.db.models import Window
from django.db.models.functions import RowNumber

from apps.clientes.services import contactos as contactos_service
from apps.energia import models as en_models
from apps.fronteras import models as fr_models

# Cuántas corridas de generación se miran para decidir si una frontera «genera
# de verdad».
CORRIDAS_VENTANA = 3


def con_relaciones():
    return (
        fr_models.Frontera.objects
        .filter(deleted_at__isnull=True)
        .select_related("proyecto", "operador_red")
        .prefetch_related("operador_red__contactos")
    )


def ultimas_generaciones(frontera_ids: list[int]) -> dict[int, list]:
    """Las últimas corridas por frontera, hasta `CORRIDAS_VENTANA`.

    **Se mira una ventana corta, no la sola corrida más reciente**: así un día
    nublado o una falla puntual del medidor no apagan la bandera de una planta
    que sigue operando. Tampoco es un umbral de días de calendario — el
    pipeline aún no corre con cadencia diaria estricta, así que se cuentan
    corridas disponibles.

    El `ROW_NUMBER()` va en SQL y no cortando en Python: una frontera con meses
    de corridas traía cientos de filas para quedarse con tres.
    """
    if not frontera_ids:
        return {}

    numeradas = (
        en_models.ReporteEnergiaGeneracion.objects
        .filter(frontera_id__in=frontera_ids)
        .annotate(fila=Window(
            expression=RowNumber(),
            partition_by=["frontera_id"],
            order_by="-fecha",
        ))
    )
    # El filtro sobre la función de ventana obliga a envolver la consulta.
    ids_validos = [
        f["id"] for f in numeradas.values("id", "fila")
        if f["fila"] <= CORRIDAS_VENTANA
    ]
    corridas = (
        en_models.ReporteEnergiaGeneracion.objects
        .filter(id__in=ids_validos).order_by("frontera_id", "-fecha")
    )

    por_frontera: dict[int, list] = {}
    for corrida in corridas:
        por_frontera.setdefault(corrida.frontera_id, []).append(corrida)
    return por_frontera


def clientes_cgm(proyecto_ids: list[int]) -> dict[int, list[dict]]:
    """`{proyecto_id: [{id, nombre, correos}]}` para el tipo `cgm`.

    Se resuelve UNA vez por proyecto distinto: varias fronteras del mismo
    proyecto —generación y consumo de la misma planta es lo habitual— repetían
    exactamente la misma consulta por cada fila.
    """
    return {
        proyecto_id: [
            {**c, "correos": contactos_service.correos("cgm", cliente_id=c["id"])}
            for c in contactos_service.clientes("cgm", proyecto_id)
        ]
        for proyecto_id in {p for p in proyecto_ids if p is not None}
    }
