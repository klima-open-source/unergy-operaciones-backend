"""API estandarizada de clasificación energética mensual (categorías a-f).

Para que cualquier área o sistema consulte el rol de cada planta en el mercado
sin reimplementar la lógica GESCON.

El snapshot se materializa en `clasificacion_energia_mensual`. Si el mes no tiene
snapshot, si lo piden con `refresh=true`, o si el que hay se calculó con reglas
viejas (`LOGICA_ACTUALIZADA_EN`), se recalcula desde GESCON/PPA en la misma
petición. **La tabla nunca se edita a mano.**
"""

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from api.exceptions import NoProcesable
from api.logging import class_logger_wrapper
from api.permissions import RolePermission
from apps.mercado_xm.models import ClasificacionEnergiaMensual
from apps.mercado_xm.services.clasificacion_energia import (
    CATEGORIAS_ENERGIA, CATEGORIAS_KEYS, recalcular_clasificacion, snapshot_obsoleto,
)
from api.v1.cumplimiento import parametros as par


def _vendedor_uso_recurso(fila) -> str | None:
    """Para la fila (c) de uso del recurso el vendedor es el CLIENTE — los
    inversionistas del proyecto—, no el mercado spot."""
    if not fila.uso_del_recurso or fila.categoria != "bolsa_compra_ungg" or not fila.proyecto:
        return None
    nombres = [
        inv.cliente.razon_social_nombre
        for inv in fila.proyecto.inversionistas.all()
        if inv.cliente and inv.cliente.razon_social_nombre
    ]
    return " / ".join(nombres) or None


@class_logger_wrapper(name="Operaciones | Clasificación energía")
class ClasificacionEnergiaViewSet(viewsets.GenericViewSet):
    """Clasificación energética mensual.

    GET /api/v1/clasificacion-energia/categorias
    GET /api/v1/clasificacion-energia?year=&month=[&categoria=][&proyecto_id=][&refresh=]

    `key` es el identificador estable para integraciones: el catálogo puede ganar
    campos, pero esas seis claves no cambian.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "head", "options"]
    queryset = ClasificacionEnergiaMensual.objects.none()

    @action(detail=False, methods=["get"], url_path="categorias")
    def categorias(self, request):
        """Catálogo estandarizado de las 6 categorías (a-f)."""
        return Response(CATEGORIAS_ENERGIA)

    def list(self, request, *args, **kwargs):
        year = par.anio(request)
        month = par.mes(request)
        categoria = request.query_params.get("categoria")
        proyecto_id = par.entero(request, "proyecto_id")

        if categoria is not None and categoria not in CATEGORIAS_KEYS:
            raise NoProcesable(
                f"Categoría desconocida: {categoria}. Válidas: {sorted(CATEGORIAS_KEYS)}"
            )

        base = ClasificacionEnergiaMensual.objects.filter(anio=year, mes=month)
        # Se recalcula si el mes no tiene snapshot, si lo piden, o si el snapshot
        # quedó con reglas viejas: sin esto un mes ya materializado seguiría
        # contradiciendo a la vista de Cumplimiento para siempre.
        if par.bandera(request, "refresh") or snapshot_obsoleto(base.first()):
            recalcular_clasificacion(year, month)

        qs = (
            ClasificacionEnergiaMensual.objects
            .filter(anio=year, mes=month)
            .select_related("proyecto", "contrato_ppa")
            .prefetch_related("proyecto__inversionistas__inversionista__cliente")
        )
        if categoria:
            qs = qs.filter(categoria=categoria)
        if proyecto_id:
            qs = qs.filter(proyecto_id=proyecto_id)
        filas = list(qs.order_by("categoria", "proyecto_id"))

        return Response({
            "year": year,
            "month": month,
            "calculado_en": filas[0].calculado_en.isoformat() if filas else None,
            "total": len(filas),
            "items": [
                {
                    "categoria": r.categoria,
                    "proyecto_id": r.proyecto_id,
                    "proyecto_nombre": r.proyecto.nombre_comercial if r.proyecto else None,
                    "contrato_ppa_id": r.contrato_ppa_id,
                    "contrato_nombre": (
                        (r.contrato_ppa.nombre_interno or r.contrato_ppa.numero_codigo_contrato)
                        if r.contrato_ppa else None
                    ),
                    "codigo_sic": r.codigo_sic,
                    "fecha_inicio": r.fecha_inicio.isoformat() if r.fecha_inicio else None,
                    "fecha_fin": r.fecha_fin.isoformat() if r.fecha_fin else None,
                    "uso_del_recurso": bool(r.uso_del_recurso),
                    "vendedor_nombre": _vendedor_uso_recurso(r),
                }
                for r in filas
            ],
        })
