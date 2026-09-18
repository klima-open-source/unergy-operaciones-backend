"""ViewSet de proyecciones de garantía (precobro XM)."""

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from datetime import date

from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.garantias import models as ga_models
from apps.garantias.services import atribucion as atribucion_service
from apps.garantias.services import proyecciones as proyecciones_service
from apps.garantias.services.calculo import KWH_PLANTA_NUEVA_DEFAULT


def _numero(request, nombre, defecto, minimo, maximo=None):
    crudo = request.query_params.get(nombre)
    if crudo in (None, ""):
        if defecto is None:
            raise ValidationError({nombre: "Requerido."})
        return defecto
    try:
        valor = float(crudo)
    except ValueError:
        raise ValidationError({nombre: "Debe ser un número."})
    if valor < minimo or (maximo is not None and valor > maximo):
        raise ValidationError({nombre: f"Entre {minimo} y {maximo}."})
    return valor


def _corte(request) -> date | None:
    """`?corte=YYYY-MM-DD`: la fecha a REPLICAR. None = el corte de hoy."""
    crudo = request.query_params.get("corte")
    if crudo in (None, ""):
        return None
    try:
        return date.fromisoformat(crudo)
    except ValueError:
        raise ValidationError({"corte": "Fecha inválida, usar YYYY-MM-DD."})


@class_logger_wrapper(name="Operaciones | Garantías | Proyecciones")
class GarantiaProyeccionViewSet(viewsets.GenericViewSet):
    """Precobro de garantía XM: cálculo en vivo y snapshot semanal.

    GET  /api/v1/garantias/proyecciones[?plantas_nuevas=&kwh_planta_nueva=&corte=YYYY-MM-DD]
    POST /api/v1/garantias/proyecciones/snapshot[?corte=]   calcula y GUARDA
    GET  /api/v1/garantias/proyecciones/historial
    GET|PUT /api/v1/garantias/proyecciones/pagado
    POST /api/v1/garantias/proyecciones/balcttos?anio=&mes=  sube el archivo

    El GET no guarda nada: es la estimación al corte de hoy. El snapshot es lo
    que congela una fila por ventana para poder comparar semanas.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ["get", "post", "put", "head", "options"]
    queryset = ga_models.GarantiaSnapshot.objects.none()

    def _parametros(self, request) -> dict:
        return {
            "plantas_nuevas": int(
                _numero(request, "plantas_nuevas", 0, 0, 10**4)
            ),
            "kwh_planta_nueva": _numero(
                request, "kwh_planta_nueva", KWH_PLANTA_NUEVA_DEFAULT, 0, 10**6
            ),
        }

    def list(self, request, *args, **kwargs):
        """Las dos estimaciones al corte. `?corte=YYYY-MM-DD` replica un corte
        pasado (precio y generación de ese día); sin él, es el de hoy. No persiste."""
        return Response(
            proyecciones_service.en_vivo(hoy=_corte(request), **self._parametros(request))
        )

    @action(detail=False, methods=["post"], url_path="snapshot")
    @log_endpoint(name="Operaciones | Garantías | Snapshot")
    def snapshot(self, request):
        # `?corte=` permite congelar un corte pasado que no se guardó en su momento.
        resultado = proyecciones_service.en_vivo(hoy=_corte(request), **self._parametros(request))
        filas = proyecciones_service.guardar_snapshot(resultado)
        return Response({
            "guardadas": len(filas),
            "fecha_corte": resultado.get("fecha_corte"),
        })

    @action(detail=False, methods=["get"], url_path="historial")
    def historial(self, request):
        return Response({"snapshots": [
            {
                "id": f.id,
                "fecha_corte": f.fecha_corte.isoformat(),
                "clave": f.clave, "anio": f.anio, "mes": f.mes,
                "neto_mwh": float(f.neto_mwh) if f.neto_mwh is not None else None,
                "precio_bolsa": (
                    float(f.precio_bolsa) if f.precio_bolsa is not None else None
                ),
                "garantia_total": (
                    float(f.garantia_total)
                    if f.garantia_total is not None else None
                ),
                "regulatorio_fallback": f.regulatorio_fallback,
            }
            for f in proyecciones_service.historial()
        ]})

    @action(detail=False, methods=["get", "put"], url_path="pagado")
    @log_endpoint(name="Operaciones | Garantías | Pagado")
    def pagado(self, request):
        if request.method == "PUT":
            anio = int(_numero(request, "anio", None, 2020, 2050))
            mes = int(_numero(request, "mes", None, 1, 12))
            valor = _numero(request, "valor", None, 0)
            proyecciones_service.set_pagado(anio, mes, valor)
            return Response({"anio": anio, "mes": mes, "valor": valor})

        return Response({"pagado": [
            {"anio": anio, "mes": mes, "valor": valor}
            for (anio, mes), valor in sorted(
                proyecciones_service.pagado_por_periodo().items()
            )
        ]})

    @action(detail=False, methods=["post"], url_path="balcttos")
    @log_endpoint(name="Operaciones | Garantías | BalCttos")
    def balcttos(self, request):
        """Recibe el BalCttos (lo empuja el agente local) y guarda su neto real.

        Ese neto MANDA sobre la proyección del balance para las ventanas del
        período: es el dato observado, no una estimación.
        """
        archivo = request.FILES.get("archivo")
        if archivo is None:
            raise ValidationError({"archivo": "Falta el archivo."})
        anio = int(_numero(request, "anio", None, 2020, 2050))
        mes = int(_numero(request, "mes", None, 1, 12))
        return Response(
            proyecciones_service.ingerir_balcttos(anio, mes, archivo.read())
        )

    @action(detail=False, methods=["get", "post"], url_path="atribucion")
    @log_endpoint(name="Operaciones | Garantías | Atribución por contrato")
    def atribucion(self, request):
        """Reparte la garantía entre los contratos que la generan.

        Solo cargan garantía los PLC (que no cubren su mínimo) o los que tienen
        duplicado; el peso sale del resumen de Cumplimiento (no de un archivo).

        POST: reparte el total que ESTIMA el modelo para el mes siguiente (M+1),
        lo guarda y lo devuelve.
        GET: el último reparto guardado de esa ventana (`?anio=&mes=`).
        """
        if request.method == "GET":
            anio = int(_numero(request, "anio", None, 2020, 2050))
            mes = int(_numero(request, "mes", None, 1, 12))
            return Response({
                "anio": anio, "mes": mes,
                "contratos": atribucion_service.leer_atribucion(anio, mes),
            })

        # Total a repartir = el que estima el modelo para el mes siguiente (M+1),
        # que es la garantía mensual que precobra XM. `?corte=` replica un corte pasado.
        resultado = proyecciones_service.en_vivo(hoy=_corte(request))
        ventana = next(
            (v for v in resultado["ventanas"] if v["clave"] == "mes_siguiente"), None
        )
        if ventana is None:
            raise ValidationError({"modelo": "El modelo no devolvió la ventana del mes siguiente."})
        total = ventana.get("garantia_total") or 0.0

        filas = atribucion_service.garantia_por_contrato(
            ventana["anio"], ventana["mes"], total
        )
        fecha_corte = date.fromisoformat(resultado["fecha_corte"])
        guardadas = atribucion_service.guardar_atribucion(
            fecha_corte, ventana["anio"], ventana["mes"], total, filas
        )
        return Response({
            "fecha_corte": resultado["fecha_corte"],
            "anio": ventana["anio"], "mes": ventana["mes"],
            "total_garantia": total,
            "guardadas": guardadas,
            "contratos": filas,
        })
