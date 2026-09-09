"""ViewSet del Reporte de Energía — 24 rutas.

El reporte diario al ASIC: clasifica cada frontera contra su medidor, sus
inversores y su histórico, deja lo dudoso marcado para revisión y, cuando no
queda nada pendiente, lo envía a Quoia.

Toda la lógica está en `apps/energia/services/reporte/` — 14 módulos, de los que
cinco (`curvas`, `datos_crudos`, `recuperacion`, `solarview`, `utils`) se movieron
sin tocar porque nunca supieron de la base.
"""

import threading
from datetime import date

from django.http import HttpResponse
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from api.exceptions import NoProcesable
from api.logging import class_logger_wrapper
from api.permissions import RolePermission
from apps.energia.models import ReporteEnergiaGeneracion
from apps.energia.services.reporte import (
    correcciones, envio, excel as excel_svc, orquestador, vistas,
)

from . import serializers as re_serializers


def _fecha(request, nombre: str = "fecha") -> date:
    """El parámetro `fecha`, obligatorio en casi todo este recurso."""
    crudo = request.query_params.get(nombre)
    try:
        return date.fromisoformat(crudo or "")
    except ValueError:
        raise NoProcesable(f"'{nombre}' es obligatorio y debe tener formato YYYY-MM-DD")


@class_logger_wrapper(name="Operaciones | Reporte de Energía")
class ReporteEnergiaViewSet(viewsets.GenericViewSet):
    """Reporte diario de energía al ASIC.

    GET  /api/v1/reporte-energia/resumen?fecha=
    GET  /api/v1/reporte-energia/resumen-historico?desde=&hasta=
    GET  /api/v1/reporte-energia/fronteras?fecha=[&tipo=&solo_pendientes=&q=]
    GET|PATCH /api/v1/reporte-energia/fronteras/{id}?fecha=
    POST /api/v1/reporte-energia/fronteras/{id}/rellenar-horario · /deshacer-relleno
    POST /api/v1/reporte-energia/fronteras/{id}/recuperar-medidor · /revisar-respaldo
    POST|DELETE /api/v1/reporte-energia/fronteras/{id}/cargar-excel-terceros
    GET  /api/v1/reporte-energia/fronteras/{id}/curva-tipica?fecha=
    POST /api/v1/reporte-energia/fronteras/{id}/validar?fecha=
    GET|POST /api/v1/reporte-energia/fronteras/{id}/exclusiones
    PATCH /api/v1/reporte-energia/exclusiones/{id} · POST …/resolver
    GET  /api/v1/reporte-energia/excel?fecha=
    POST /api/v1/reporte-energia/ejecutar · /ejecutar/cancelar
    GET  /api/v1/reporte-energia/ejecutar/estado
    POST /api/v1/reporte-energia/enviar?fecha=
    GET|POST /api/v1/reporte-energia/estado-quoia?fecha=

    **`/enviar` está bloqueado mientras quede una frontera sin validar.** El
    reporte es del día completo.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    queryset = ReporteEnergiaGeneracion.objects.none()

    # ── Resumen y listado ─────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="resumen")
    def resumen(self, request):
        return Response(vistas.resumen(_fecha(request)))

    @action(detail=False, methods=["get"], url_path="resumen-historico")
    def resumen_historico(self, request):
        return Response(vistas.resumen_historico(
            _fecha(request, "desde"), _fecha(request, "hasta"),
        ))

    @action(detail=False, methods=["get"], url_path="fronteras")
    def fronteras(self, request):
        crudo = request.query_params.get("solo_pendientes", "")
        return Response(vistas.listar_fronteras(
            _fecha(request),
            tipo=request.query_params.get("tipo"),
            solo_pendientes=crudo.strip().lower() in ("1", "true", "yes", "on"),
            q=request.query_params.get("q"),
        ))

    # ── Detalle y corrección de UNA frontera ──────────────────────────────

    @action(
        detail=False, methods=["get", "patch"],
        url_path=r"fronteras/(?P<frontera_id>\d+)",
    )
    def frontera(self, request, frontera_id=None):
        fecha = _fecha(request)
        if request.method == "GET":
            return Response(vistas._construir_detalle(int(frontera_id), fecha))

        entrada = re_serializers.EditarCurvaSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        return Response(correcciones.editar_curva(
            int(frontera_id), fecha, dict(entrada.validated_data),
        ))

    @action(
        detail=False, methods=["post"],
        url_path=r"fronteras/(?P<frontera_id>\d+)/rellenar-horario",
    )
    def rellenar_horario(self, request, frontera_id=None):
        """Rellena a mano las horas sin dato. Es una acción EXPLÍCITA: ya no
        pasa sola durante la clasificación automática (mezclar otra fuente en la
        curva final sin que nadie lo pidiera era demasiado invasivo)."""
        return Response(correcciones.rellenar_horario(int(frontera_id), _fecha(request)))

    @action(
        detail=False, methods=["post"],
        url_path=r"fronteras/(?P<frontera_id>\d+)/deshacer-relleno",
    )
    def deshacer_relleno(self, request, frontera_id=None):
        return Response(correcciones.deshacer_relleno(int(frontera_id), _fecha(request)))

    @action(
        detail=False, methods=["post"],
        url_path=r"fronteras/(?P<frontera_id>\d+)/recuperar-medidor",
    )
    def recuperar_medidor(self, request, frontera_id=None):
        """Interroga los DOS medidores por WebSocket (hasta 90 s cada uno).

        No toca `curva_final`, `medidor_usado`, `caso` ni
        `editado_manualmente`: solo refresca datos de referencia, por eso no
        necesita ningún guard de "no pisar lo ya editado".
        """
        return Response(correcciones.recuperar_medidor(int(frontera_id), _fecha(request)))

    @action(
        detail=False, methods=["post"],
        url_path=r"fronteras/(?P<frontera_id>\d+)/revisar-respaldo",
    )
    def revisar_respaldo(self, request, frontera_id=None):
        """Adopta el valor en vivo del respaldo si pasa la tolerancia de
        coherencia. Liviano: NO interroga el dispositivo."""
        return Response(correcciones.revisar_respaldo(int(frontera_id), _fecha(request)))

    @action(
        detail=False, methods=["post", "delete"],
        url_path=r"fronteras/(?P<frontera_id>\d+)/cargar-excel-terceros",
    )
    def excel_terceros(self, request, frontera_id=None):
        """El Excel que envía la empresa tercera que hace el CGM de esta
        frontera, en vez de transcribirlo a mano en Quoia."""
        if request.method == "DELETE":
            return Response(
                correcciones.eliminar_excel_terceros(int(frontera_id), _fecha(request))
            )
        archivo = request.FILES.get("archivo")
        if archivo is None:
            raise ValidationError("Falta el archivo")
        return Response(
            correcciones.cargar_excel_terceros(int(frontera_id), archivo.read())
        )

    @action(
        detail=False, methods=["get"],
        url_path=r"fronteras/(?P<frontera_id>\d+)/curva-tipica",
    )
    def curva_tipica(self, request, frontera_id=None):
        """Mediana × forma horaria de los últimos días confiables. **No guarda
        nada**: es para que la persona la revise antes de guardar la corrección."""
        return Response(correcciones.curva_tipica(int(frontera_id), _fecha(request)))

    @action(
        detail=False, methods=["post"],
        url_path=r"fronteras/(?P<frontera_id>\d+)/validar",
    )
    def validar(self, request, frontera_id=None):
        return Response(
            correcciones.validar(int(frontera_id), _fecha(request), request.user)
        )

    # ── Exclusiones temporales ────────────────────────────────────────────

    @action(
        detail=False, methods=["get", "post"],
        url_path=r"fronteras/(?P<frontera_id>\d+)/exclusiones",
    )
    def exclusiones(self, request, frontera_id=None):
        if request.method == "GET":
            return Response(correcciones.listar_exclusiones(int(frontera_id)))
        entrada = re_serializers.CrearExclusionSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        return Response(correcciones.crear_exclusion(
            int(frontera_id), dict(entrada.validated_data), request.user,
        ))

    @action(
        detail=False, methods=["patch"],
        url_path=r"exclusiones/(?P<exclusion_id>\d+)",
    )
    def exclusion(self, request, exclusion_id=None):
        entrada = re_serializers.EditarExclusionSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        return Response(correcciones.editar_exclusion(
            int(exclusion_id), dict(entrada.validated_data),
        ))

    @action(
        detail=False, methods=["post"],
        url_path=r"exclusiones/(?P<exclusion_id>\d+)/resolver",
    )
    def resolver_exclusion(self, request, exclusion_id=None):
        return Response(correcciones.resolver_exclusion(int(exclusion_id)))

    # ── Excel, corrida y envío ────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="excel")
    def excel(self, request):
        """El Excel en formato manual. Disponible SIEMPRE, sin restricciones —
        a diferencia de `/enviar`, que sí se bloquea con pendientes."""
        fecha = _fecha(request)
        respuesta = HttpResponse(
            excel_svc.generar_excel_dia(fecha),
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        respuesta["Content-Disposition"] = (
            f'attachment; filename="reporte-energia-{fecha}.xlsx"'
        )
        return respuesta

    @action(detail=False, methods=["post"], url_path="ejecutar")
    def ejecutar(self, request):
        """Dispara la clasificación del día en un hilo aparte y responde de
        inmediato: con ~50 fronteras la corrida tarda varios minutos, más que el
        timeout del proxy que usa el frontend.

        El rechazo por "ya hay una corrida en curso" se comprueba ACÁ, antes de
        lanzar el hilo: adentro nadie lo vería, porque la respuesta ya se fue
        con "iniciado". `cancelable=True` -- las que lanza una persona sí se
        pueden detener; la de Celery no (ver orquestador.ejecutar_dia).
        """
        fecha = _fecha(request)
        en_curso = orquestador.corrida_en_curso(fecha)
        if en_curso:
            cual = "automática de la madrugada" if en_curso.get("origen") == "automatica" else "manual"
            raise NoProcesable(
                f"Ya hay una corrida {cual} en curso para esa fecha (desde "
                f"{en_curso.get('desde', '?')}). Espera a que termine para no "
                f"escribir las mismas filas dos veces."
            )
        threading.Thread(
            target=orquestador.ejecutar_dia_background, args=(fecha,),
            kwargs={"cancelable": True}, daemon=True,
        ).start()
        return Response({"fecha": fecha, "status": "iniciado"})

    @action(detail=False, methods=["get"], url_path="ejecutar/estado")
    def ejecutar_estado(self, request):
        """`fallidas`/`omitidas` van siempre, incluso sin corrida registrada:
        el frontend hace `data.fallidas.length` para decidir si avisa, y una
        respuesta sin la clave le tiraba un TypeError que se comía un catch
        silencioso -- el mismo silencio que este endpoint existe para romper.
        """
        fecha = _fecha(request)
        resultado = orquestador.ultima_corrida(fecha)
        return Response({
            "fecha": fecha, "fallidas": [], "omitidas": [], **(resultado or {}),
        })

    @action(detail=False, methods=["post"], url_path="ejecutar/cancelar")
    def ejecutar_cancelar(self, request):
        """Cooperativo, no inmediato: el bucle revisa la bandera ENTRE fronteras
        y nunca corta a media frontera."""
        fecha = _fecha(request)
        orquestador.cancelar_corrida(fecha)
        return Response({"fecha": fecha, "solicitado": True})

    @action(detail=False, methods=["post"], url_path="enviar")
    def enviar(self, request):
        return Response(envio.enviar(_fecha(request)))

    @action(detail=False, methods=["get", "post"], url_path="estado-quoia")
    def estado_quoia(self, request):
        """GET devuelve lo YA guardado; POST fuerza una revisión en vivo contra
        Quoia, solo para las que siguen en espera."""
        fecha = _fecha(request)
        if request.method == "GET":
            return Response(envio.estado_quoia_actual(fecha))
        return Response(envio.estado_quoia_revisar(fecha))
