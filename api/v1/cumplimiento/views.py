"""ViewSet de Cumplimiento — 24 rutas, todas sobre el mismo dominio.

El módulo original (`app/api/v1/cumplimiento.py`) eran 3 805 líneas con la lógica
de negocio dentro de los endpoints y la sesión de SQLAlchemy atravesándolo todo:
otros servicios lo llamaban pasándole `db=..., _=None`, es decir, invocaban una
VISTA para obtener datos.

Acá la vista no calcula nada. Todo vive en
`apps/mercado_xm/services/cumplimiento/`, un módulo por tema, y lo que antes era
`get_plantas_contratos(...)` es ahora `piscinas.plantas_contratos(...)`: un
servicio que llaman otros servicios sin fingir una petición HTTP.
"""

from datetime import date

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from api.exceptions import NoProcesable
from api.logging import class_logger_wrapper
from api.permissions import RolePermission
from apps.energia.services import comercializacion
from apps.mercado_xm.models import CumplimientoMensual
from apps.mercado_xm.services.cumplimiento import (
    cierre, contratos as contratos_svc, descubrimientos as desc_svc,
    detalle, diagnostico, listados, panel, piscinas, resumen as resumen_svc,
    simulador as simulador_svc, transada, vista_contratos,
)
from apps.mercado_xm.services.cumplimiento.balance_energia import calcular_balance

from . import parametros as par
from . import serializers as cu_serializers


@class_logger_wrapper(name="Operaciones | Cumplimiento")
class CumplimientoViewSet(viewsets.GenericViewSet):
    """Cumplimiento contractual de energía (PPA × GESCON × generación real).

    GET  /api/v1/cumplimiento/ppa
    GET  /api/v1/cumplimiento/ppa/resumen?year=&month=
    GET  /api/v1/cumplimiento/ppa/resumen-anual?year=
    GET  /api/v1/cumplimiento/ppa/{contrato_id}?year=&month=
    GET  /api/v1/cumplimiento/ppa/{contrato_id}/anual?year=
    GET  /api/v1/cumplimiento/ppa/{contrato_id}/plantas-inscritas-por-mes
    GET  /api/v1/cumplimiento/simulador?year=&month=
    GET  /api/v1/cumplimiento/vista-contratos?fecha=
    GET  /api/v1/cumplimiento/plantas-contratos?year=&month=
    GET  /api/v1/cumplimiento/balance-energia?year=&month=
    POST /api/v1/cumplimiento/backfill-comercializacion[?dry_run=&force=]
    GET  /api/v1/cumplimiento/sin-fecha-comercializacion
    GET  /api/v1/cumplimiento/energia-transada?year=&month=
    GET  /api/v1/cumplimiento/anual-matriz?year=
    GET  /api/v1/cumplimiento/anual-matriz/contratos?year=
    GET  /api/v1/cumplimiento/anual-matriz/contrato/{contrato_id}?year=
    GET  /api/v1/cumplimiento/descubrimientos?year=&month_from=&month_to=
    POST /api/v1/cumplimiento/cerrar-periodo
    GET  /api/v1/cumplimiento/historico[?filtros]
    GET  /api/v1/cumplimiento/historico/{record_id}
    POST /api/v1/cumplimiento/historico/{record_id}/facturar
    GET  /api/v1/cumplimiento/diagnostico
    POST /api/v1/cumplimiento/fix-enlaces
    GET  /api/v1/cumplimiento/panel-anual?year=

    **`/plantas-contratos` es el núcleo**: resuelve vigencias GESCON, relevos y
    recortes intra-mes, y de él salen `/vista-contratos`, `/balance-energia` y la
    clasificación energética. Ninguno reimplementa esa lógica.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "head", "options"]
    queryset = CumplimientoMensual.objects.none()
    lookup_value_regex = r"\d+"

    # ── Contratos ─────────────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="ppa")
    def ppa(self, request):
        return Response(listados.listar_contratos())

    @action(detail=False, methods=["get"], url_path="ppa/resumen")
    def ppa_resumen(self, request):
        return Response(resumen_svc.resumen(
            par.anio(request), par.mes(request),
        ))

    @action(detail=False, methods=["get"], url_path="ppa/resumen-anual")
    def ppa_resumen_anual(self, request):
        return Response(listados.resumen_anual(
            par.anio(request),
        ))

    @action(detail=False, methods=["get"], url_path=r"ppa/(?P<contrato_id>\d+)")
    def ppa_detalle(self, request, contrato_id=None):
        return Response(detalle.cumplimiento_de_contrato(
            int(contrato_id), par.anio(request), par.mes(request),
        ))

    @action(detail=False, methods=["get"], url_path=r"ppa/(?P<contrato_id>\d+)/anual")
    def ppa_anual(self, request, contrato_id=None):
        return Response(contratos_svc.anual_de_contrato(int(contrato_id), par.anio(request)))

    @action(
        detail=False, methods=["get"],
        url_path=r"ppa/(?P<contrato_id>\d+)/plantas-inscritas-por-mes",
    )
    def ppa_plantas_inscritas(self, request, contrato_id=None):
        return Response(contratos_svc.plantas_inscritas_por_mes(int(contrato_id)))

    # ── Vistas del mes ────────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="simulador")
    def simulador(self, request):
        return Response(simulador_svc.simulador(
            par.anio(request), par.mes(request),
        ))

    @action(detail=False, methods=["get"], url_path="vista-contratos")
    def vista_contratos(self, request):
        """La foto de UN día, con todos los contratos de venta vigentes."""
        crudo = request.query_params.get("fecha")
        try:
            dia = date.fromisoformat(crudo or "")
        except ValueError:
            raise NoProcesable(f"'{crudo}' no es una fecha válida. Usá el formato YYYY-MM-DD.")

        return Response(vista_contratos.construir(fecha=dia))

    @action(detail=False, methods=["get"], url_path="plantas-contratos")
    def plantas_contratos(self, request):
        return Response(piscinas.plantas_contratos(
            par.anio(request), par.mes(request),
        ))

    @action(detail=False, methods=["get"], url_path="balance-energia")
    def balance_energia(self, request):
        return Response(calcular_balance(
            par.anio(request), par.mes(request),
            excluir_compra_externa=par.bandera(request, "excluir_compra_externa"),
        ))

    @action(detail=False, methods=["get"], url_path="energia-transada")
    def energia_transada(self, request):
        return Response(transada.energia_transada(
            par.anio(request), par.mes(request),
        ))

    # ── Comercialización ──────────────────────────────────────────────────

    @action(detail=False, methods=["post"], url_path="backfill-comercializacion")
    def backfill_comercializacion(self, request):
        """`dry_run` es true por defecto: no escribe hasta que lo pidan."""
        return Response(comercializacion.backfill_comercializacion(
            force=par.bandera(request, "force"),
            dry_run=par.bandera(request, "dry_run", defecto=True),
        ))

    @action(detail=False, methods=["get"], url_path="sin-fecha-comercializacion")
    def sin_fecha_comercializacion(self, request):
        filas = comercializacion.proyectos_sin_fecha_comercializacion()
        return Response({"total": len(filas), "proyectos": filas})

    # ── Matriz anual ──────────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="anual-matriz")
    def anual_matriz(self, request):
        return Response(resumen_svc.anual_matriz(
            par.anio(request),
        ))

    @action(detail=False, methods=["get"], url_path="anual-matriz/contratos")
    def anual_matriz_contratos(self, request):
        return Response(contratos_svc.anual_matriz_contratos(
            par.anio(request),
        ))

    @action(
        detail=False, methods=["get"],
        url_path=r"anual-matriz/contrato/(?P<contrato_id>\d+)",
    )
    def anual_matriz_contrato(self, request, contrato_id=None):
        return Response(contratos_svc.anual_matriz_contrato(int(contrato_id), par.anio(request)))

    @action(detail=False, methods=["get"], url_path="panel-anual")
    def panel_anual(self, request):
        return Response(panel.panel_anual(
            par.anio(request),
            incluir_plantas=par.bandera(request, "incluir_plantas", defecto=True),
            refrescar=par.bandera(request, "refrescar"),
        ))

    # ── Exposición y cierre ───────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="descubrimientos")
    def descubrimientos(self, request):
        return Response(desc_svc.descubrimientos(
            par.anio(request),
            par.entero(request, "month_from", 1, 1, 12),
            par.entero(request, "month_to", 12, 1, 12),
        ))

    @action(detail=False, methods=["post"], url_path="cerrar-periodo")
    def cerrar_periodo(self, request):
        entrada = cu_serializers.CerrarPeriodoSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        return Response(cierre.cerrar_periodo(**entrada.validated_data))

    @action(detail=False, methods=["get"], url_path="historico")
    def historico(self, request):
        return Response(cierre.historico(
            contrato_id=par.entero(request, "contrato_id"),
            proyecto_id=par.entero(request, "proyecto_id"),
            anio=par.entero(request, "anio", None, 2020, 2050),
            mes=par.entero(request, "mes", None, 1, 12),
            estado=request.query_params.get("estado"),
        ))

    @action(detail=False, methods=["get"], url_path=r"historico/(?P<record_id>\d+)")
    def historico_detalle(self, request, record_id=None):
        return Response(cierre.historico_detalle(int(record_id)))

    @action(
        detail=False, methods=["post"],
        url_path=r"historico/(?P<record_id>\d+)/facturar",
    )
    def facturar(self, request, record_id=None):
        entrada = cu_serializers.FacturarSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        return Response(cierre.facturar(
            int(record_id), entrada.validated_data.get("liquidacion_id"),
        ))

    # ── Diagnóstico ───────────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="diagnostico")
    def diagnostico(self, request):
        return Response(diagnostico.diagnostico_enlaces())

    @action(detail=False, methods=["post"], url_path="fix-enlaces")
    def fix_enlaces(self, request):
        """Corrección puntual, limitada a un correo concreto (viene del original)."""
        return Response(diagnostico.fix_enlaces(getattr(request.user, "email", "")))
