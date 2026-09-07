"""ViewSet de Fallas — 19 rutas.

El módulo original tenía las 1 457 líneas de lógica dentro de los endpoints. Acá
la vista valida, llama al servicio y responde; las reglas (SLA, clasificación
estructurada, bloqueo de cierre) viven en
`apps/monitoreo/services/fallas/dominio.py`, que es su único dueño.

`/fallas/por-proyecto` es la puerta para integradores externos con API Key: usa
las tres cubetas (`vigente` / `programado` / `terminado`) en vez de los seis
estados internos, y resuelve la planta por id, `sub_project` o nombre exacto.
"""

import io
import json
import logging
import os
import uuid
from datetime import datetime, time, timezone

from django.db import IntegrityError, transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from api.exceptions import ServicioNoDisponible
from api.logging import class_logger_wrapper
from api.pagination import PaginacionConPaginas
from api.permissions import RolePermission
from api.v1.cumplimiento import parametros as par
from apps.monitoreo import models as mo_models
from apps.monitoreo.services.fallas import consultas, dominio
from apps.monitoreo.services.fallas.estructura import ESTRUCTURA_FALLAS

from . import serializers as fa_serializers

logger = logging.getLogger("operaciones.fallas")

FALLA_MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB
DRIVE_ROOT_FOLDER_ID = "0AD_e3wIWHByDUk9PVA"


class PaginacionFallas(PaginacionConPaginas):
    """20/5000, los limites que declaraba `app/api/v1/fallas.py:609-611`.

    No se cambia `api.pagination` porque sus 50/500 son el default de los otros
    46 recursos, y en FastAPI cada listado traia sus propios limites.
    """

    page_size = 20
    max_page_size = 5000


def _drive():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise ServicioNoDisponible(
            "Google Drive no configurado (falta GOOGLE_SERVICE_ACCOUNT_JSON)"
        )
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json), scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _carpeta(service, nombre: str, padre_id: str) -> str:
    q = (f"name='{nombre}' and mimeType='application/vnd.google-apps.folder' "
         f"and '{padre_id}' in parents and trashed=false")
    res = service.files().list(
        q=q, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    if res.get("files"):
        return res["files"][0]["id"]
    meta = {"name": nombre, "mimeType": "application/vnd.google-apps.folder",
            "parents": [padre_id]}
    return service.files().create(body=meta, fields="id", supportsAllDrives=True).execute()["id"]


def _fotos_como_objetos(lista: list) -> list[dict]:
    """Normaliza `fotos_urls` a objetos completos.

    Soporta el formato legado (strings `"url#nombre"`) y el nuevo (dicts).
    """
    salida = []
    for item in lista:
        if isinstance(item, dict):
            salida.append(item)
        elif isinstance(item, str):
            if "#" in item:
                url, nombre = item.rsplit("#", 1)
            else:
                url, nombre = item, item.split("/")[-1]
            salida.append({
                "id": url.split("/d/")[-1].split("/")[0] if "/d/" in url else uuid.uuid4().hex,
                "nombre": nombre,
                "url": url,
                "tamaño": None,
                "tipo_mime": None,
                "created_at": None,
            })
    return salida


@class_logger_wrapper(name="Operaciones | Fallas")
class FallaViewSet(viewsets.GenericViewSet):
    """Fallas de las plantas: registro, seguimiento y SLA.

    GET  /api/v1/fallas/sla-dashboard · /catalogos · /estructura
    GET  /api/v1/fallas/stats/resumen · /actividad-hoy
    GET|POST /api/v1/fallas
    POST /api/v1/fallas/backfill-sla
    GET|PATCH|DELETE /api/v1/fallas/{id}
    POST /api/v1/fallas/{id}/notificar · /{id}/seguimientos
    GET  /api/v1/fallas/{id}/impacto · /{id}/archivos
    POST /api/v1/fallas/{id}/archivos · /{id}/attachments
    DELETE /api/v1/fallas/{id}/archivos/{archivo_id}

    **Una falla nunca se borra físico**: `deleted_at`. Y no se puede cerrar
    mientras siga pendiente de reclasificar — ver
    `dominio.bloquear_cierre_si_pendiente`.
    """

    permission_classes = [RolePermission]
    pagination_class = PaginacionFallas
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    queryset = mo_models.Falla.objects.filter(deleted_at__isnull=True)
    lookup_value_regex = r"\d+"

    def get_serializer_class(self):
        if self.action == "create":
            return fa_serializers.FallaCrearSerializer
        if self.action == "partial_update":
            return fa_serializers.FallaActualizarSerializer
        if self.action == "list":
            return fa_serializers.FallaListaSerializer
        return fa_serializers.FallaSerializer

    def _falla(self, pk) -> mo_models.Falla:
        falla = consultas.base_detalle().filter(pk=pk).first()
        if not falla:
            raise NotFound("Falla no encontrada")
        return falla

    def _falla_simple(self, pk) -> mo_models.Falla:
        falla = mo_models.Falla.objects.filter(pk=pk, deleted_at__isnull=True).first()
        if not falla:
            raise NotFound("Falla no encontrada")
        return falla

    # ── Tableros y catálogos ──────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="sla-dashboard")
    def sla_dashboard(self, request):
        return Response(consultas.sla_dashboard())

    @action(detail=False, methods=["get"], url_path="catalogos")
    def catalogos(self, request):
        return Response({
            "estados": fa_serializers.FallaCatEstadoSerializer(
                mo_models.FallaCatEstado.objects.order_by("orden"), many=True).data,
            "prioridades": fa_serializers.FallaCatPrioridadSerializer(
                mo_models.FallaCatPrioridad.objects.order_by("nivel"), many=True).data,
            "tipos": fa_serializers.FallaCatTipoSerializer(
                mo_models.FallaCatTipo.objects.filter(activa=True)
                .select_related("categoria").order_by("etiqueta"), many=True).data,
            "resoluciones": fa_serializers.FallaCatResolucionSerializer(
                mo_models.FallaCatResolucion.objects.order_by("etiqueta"), many=True).data,
        })

    @action(detail=False, methods=["get"], url_path="estructura")
    def estructura(self, request):
        """La jerarquía canónica sistema → opciones/equipos/tipos. Fuente única
        que consumen el formulario web y la app móvil."""
        return Response({"categorias": ESTRUCTURA_FALLAS})

    @action(detail=False, methods=["get"], url_path="stats/resumen")
    def stats_resumen(self, request):
        return Response(consultas.stats_resumen())

    @action(detail=False, methods=["get"], url_path="actividad-hoy")
    def actividad_hoy(self, request):
        """Fallas creadas hoy y fallas que cambiaron de estado hoy (hora Colombia)."""
        fecha, creadas, cambios, fallas_map = consultas.actividad_hoy()
        serializar = fa_serializers.FallaSerializer
        return Response({
            "fecha": fecha,
            "creadas": serializar(creadas, many=True).data,
            "cambios_estado": [
                {
                    "falla": serializar(fallas_map[c["falla_id"]]).data,
                    "estado_anterior": c["estado_anterior"],
                    "estado_nuevo": c["estado_nuevo"],
                    "hora": c["hora"],
                }
                for c in cambios
            ],
        })

    # ── Listado y creación ────────────────────────────────────────────────

    def list(self, request, *args, **kwargs):
        parametros = {
            "q": request.query_params.get("q"),
            "buscar": request.query_params.get("buscar"),
            "estado_id": par.entero(request, "estado_id"),
            "estado_codigo": request.query_params.get("estado_codigo"),
            "prioridad_id": par.entero(request, "prioridad_id"),
            "prioridad_codigo": request.query_params.get("prioridad_codigo"),
            "tipo_codigo": request.query_params.get("tipo_codigo"),
            "proyecto_id": par.entero(request, "proyecto_id"),
            "cliente_id": par.entero(request, "cliente_id"),
            "solo_alerta": par.bandera(request, "solo_alerta"),
            "solo_activas": par.bandera(request, "solo_activas"),
            "activa_en_fecha": request.query_params.get("activa_en_fecha"),
            "fecha_programada_desde": request.query_params.get("fecha_programada_desde"),
            "fecha_programada_hasta": request.query_params.get("fecha_programada_hasta"),
            "con_fecha_programada": par.bandera(request, "con_fecha_programada"),
            "pendiente_reclasificar": (
                None if request.query_params.get("pendiente_reclasificar") is None
                else par.bandera(request, "pendiente_reclasificar")
            ),
        }
        # `page_size` es un alias histórico de `size` solo en este listado.
        alias = request.query_params.get("page_size")
        if alias and not request.query_params.get("size"):
            request._request.GET = request._request.GET.copy()
            request._request.GET["size"] = alias
        pagina = self.paginate_queryset(consultas.filtrar(parametros))
        return self.get_paginated_response(
            fa_serializers.FallaListaSerializer(pagina, many=True).data
        )

    def create(self, request, *args, **kwargs):
        entrada = self.get_serializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = dict(entrada.validated_data)
        intervalos = datos.pop("intervalos", None)
        inversores = datos.pop("inversores", None)
        generar_impacto = datos.pop("generar_impacto", False)
        fotos = datos.pop("fotos_urls", None)

        # Camino estructurado: validar ANTES de crear nada.
        categoria = datos.get("categoria_codigo")
        if categoria:
            dominio.validar_payload(categoria, datos.get("subtipo_codigo"), inversores)

        try:
            with transaction.atomic():
                falla = mo_models.Falla.objects.create(
                    **datos,
                    codigo_interno=f"TMP-{uuid.uuid4().hex[:12]}",
                    registrado_por_id=request.user.id,
                    fotos_urls=fotos or None,
                )
                # El código definitivo usa el id ya asignado: así no hay
                # colisiones entre dos creaciones simultáneas.
                falla.codigo_interno = f"FAL-{datetime.now(timezone.utc).year}-{falla.id:05d}"
                dominio.sincronizar_intervalos(falla, intervalos)
                if categoria:
                    dominio.aplicar_clasificacion(falla, inversores or [])
                falla.save()
        except IntegrityError as exc:
            raise dominio.integridad_a_error(exc)

        self._notificar_coordinadores(falla)
        self._alarmas_post_guardado(falla.id)
        if generar_impacto:
            self._generar_impacto(falla, request.user)

        return Response(
            fa_serializers.FallaSerializer(self._falla(falla.id)).data,
            status=status.HTTP_201_CREATED,
        )

    def _notificar_coordinadores(self, falla) -> None:
        from apps.plataforma.models import Notificacion, Usuario

        coordinadores = Usuario.objects.filter(rol="coordinador", activo=True)
        nombre = falla.proyecto.nombre_comercial if falla.proyecto_id else f"Proyecto {falla.proyecto_id}"
        Notificacion.objects.bulk_create([
            Notificacion(
                usuario_id=c.id,
                tipo="accion",
                titulo="Nueva falla registrada",
                mensaje=f"{falla.codigo_interno} — {nombre}: {(falla.descripcion or '')[:80]}",
                leida=False,
            )
            for c in coordinadores
        ])

    def _alarmas_post_guardado(self, falla_id: int) -> None:
        """Alarmas de comunicación tras crear o actualizar. Nunca rompe el flujo."""
        try:
            from apps.monitoreo.services.fallas.alarmas import evaluar_alarmas_falla

            falla = mo_models.Falla.objects.select_related("proyecto").filter(pk=falla_id).first()
            if falla and falla.categoria_codigo:
                evaluar_alarmas_falla(falla)
        except Exception:
            logger.exception("evaluar_alarmas_falla falló (no bloqueante)")

    def _generar_impacto(self, falla, usuario) -> None:
        """Crea un `MantenimientoImpacto` ligado a la falla usando su ventana.

        Silenciosa ante errores: nunca debe tumbar la creación de la falla.
        """
        try:
            from apps.monitoreo.services.impacto import calcular

            inicio = falla.fecha_ocurrencia
            if inicio is None and falla.fecha_identificacion:
                inicio = datetime.combine(
                    falla.fecha_identificacion,
                    falla.hora_identificacion or time(0, 0),
                    tzinfo=dominio._COL_TZ,
                )
            if inicio is None:
                return
            fin = falla.fecha_resolucion or datetime.now(dominio._COL_TZ)
            if inicio.tzinfo is None:
                inicio = inicio.replace(tzinfo=dominio._COL_TZ)
            if fin.tzinfo is None:
                fin = fin.replace(tzinfo=dominio._COL_TZ)
            fin = max(fin, inicio)

            metricas = calcular(falla.proyecto_id, inicio, fin)
            # `metricas` trae también `precio_cop_kwh`, que no es columna.
            columnas = (
                "expected_generation_kwh", "actual_generation_kwh",
                "lost_energy_kwh", "financial_impact_cop", "ppa_penalty_risk_flag",
            )
            mo_models.MantenimientoImpacto.objects.create(
                proyecto_id=falla.proyecto_id,
                falla_id=falla.id,
                maintenance_type="unscheduled",  # nace de una falla → no programado
                start_time=inicio,
                end_time=fin,
                created_by=getattr(usuario, "id", None),
                **{c: metricas[c] for c in columnas},
            )
        except Exception:
            logger.warning(
                "No se pudo generar impacto de mantenimiento para falla %s",
                falla.id, exc_info=True,
            )

    @action(detail=False, methods=["post"], url_path="backfill-sla")
    def backfill_sla(self, request):
        """Recalcula `sla_cumplido` para todas las fallas resueltas, ahora que el
        campo es siempre calculado. `dry_run=true` solo reporta qué cambiaría."""
        return Response(consultas.backfill_sla_cumplido(
            dry_run=par.bandera(request, "dry_run", defecto=True)
        ))

    # ── Detalle ───────────────────────────────────────────────────────────

    def retrieve(self, request, pk=None):
        return Response(fa_serializers.FallaSerializer(self._falla(pk)).data)

    def partial_update(self, request, pk=None):
        falla = self._falla_simple(pk)
        entrada = self.get_serializer(falla, data=request.data, partial=True)
        entrada.is_valid(raise_exception=True)
        datos = dict(entrada.validated_data)

        toca_intervalos = "intervalos" in request.data
        intervalos = datos.pop("intervalos", None)
        toca_inversores = "inversores" in request.data
        inversores = datos.pop("inversores", None)

        for campo, valor in datos.items():
            setattr(falla, campo, valor)

        try:
            with transaction.atomic():
                if toca_intervalos:
                    dominio.sincronizar_intervalos(falla, intervalos or [])
                self._reclasificar(falla, datos, inversores, toca_inversores)

                # Sella fecha+hora de solución y calcula `sla_cumplido` al cerrar;
                # al reabrir limpia ambos. Ver `dominio.sincronizar_resolucion`.
                if datos.get("estado_id") is not None:
                    nuevo_estado = mo_models.FallaCatEstado.objects.filter(
                        pk=datos["estado_id"]
                    ).first()
                    dominio.bloquear_cierre_si_pendiente(falla, nuevo_estado)
                    dominio.sincronizar_resolucion(falla, nuevo_estado)
                falla.save()
        except IntegrityError as exc:
            raise dominio.integridad_a_error(exc)

        self._alarmas_post_guardado(int(pk))
        return Response(fa_serializers.FallaSerializer(self._falla(pk)).data)

    def _reclasificar(self, falla, datos, inversores, toca_inversores) -> None:
        """Revalida y recalcula lo derivado si se tocó la categoría o los inversores."""
        campos_estructura = (
            "categoria_codigo", "subtipo_codigo", "subtipo_detalle",
            "frontera_afecta_medicion", "frontera_perdida_comunicacion",
        )
        toca_estructura = toca_inversores or any(c in datos for c in campos_estructura)

        if "categoria_codigo" in datos and datos["categoria_codigo"] is None:
            # Limpieza explícita. Sin este bloque, el guard de abajo nunca es
            # cierto para este caso y todo lo derivado quedaba congelado con el
            # valor viejo, contradiciendo el `categoria_codigo: null` recién
            # guardado (auditoría 2026-09-02).
            falla.subtipo_codigo = None
            falla.subtipo_detalle = None
            falla.pendiente_reclasificar = False
            falla.frontera_afecta_medicion = None
            falla.frontera_perdida_comunicacion = None
            falla.inversores_perdida_comunicacion = None
            falla.clasificacion = None
            if "tipo_id" not in datos:
                falla.tipo_id = None
            mo_models.FallaInversor.objects.filter(falla_id=falla.id).delete()
        elif toca_estructura and falla.categoria_codigo:
            # Para inversores solo se valida si llegó lista nueva: si no, los
            # datos existentes ya eran válidos.
            if falla.categoria_codigo == "inversores":
                if toca_inversores:
                    dominio.validar_payload(
                        falla.categoria_codigo, falla.subtipo_codigo, inversores
                    )
            else:
                dominio.validar_payload(falla.categoria_codigo, falla.subtipo_codigo, None)
            dominio.aplicar_clasificacion(
                falla, inversores if toca_inversores else None
            )

    def destroy(self, request, pk=None):
        falla = self._falla_simple(pk)
        falla.deleted_at = datetime.now(timezone.utc)
        falla.save(update_fields=["deleted_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"], url_path="notificar")
    def notificar(self, request, pk=None):
        """Envía la notificación por correo. Nunca lanza: la falla ya está guardada."""
        from apps.monitoreo.services.fallas.notificacion import enviar_notificacion

        falla = self._falla(pk)
        accion = "cerrada" if falla.estado and falla.estado.es_estado_final else "creada"
        return Response(enviar_notificacion(
            falla, accion=accion, usuario_nombre=getattr(request.user, "nombre", ""),
        ))

    @action(detail=True, methods=["post"], url_path="seguimientos")
    def seguimientos(self, request, pk=None):
        falla = self._falla_simple(pk)
        entrada = fa_serializers.FallaSeguimientoEntradaSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data

        with transaction.atomic():
            if datos.get("estado_nuevo_id"):
                nuevo_estado = mo_models.FallaCatEstado.objects.filter(
                    pk=datos["estado_nuevo_id"]
                ).first()
                dominio.bloquear_cierre_si_pendiente(falla, nuevo_estado)
                falla.estado_id = datos["estado_nuevo_id"]
                dominio.sincronizar_resolucion(falla, nuevo_estado)
                falla.save()
            seguimiento = mo_models.FallaSeguimiento.objects.create(
                falla_id=falla.id,
                usuario_id=request.user.id,
                nota=datos.get("nota"),
                estado_nuevo_id=datos.get("estado_nuevo_id"),
            )

        completo = (
            mo_models.FallaSeguimiento.objects
            .select_related("usuario", "estado_nuevo")
            .get(pk=seguimiento.id)
        )
        return Response(
            fa_serializers.FallaSeguimientoSerializer(completo).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"], url_path="impacto")
    def impacto(self, request, pk=None):
        """Pérdida de generación e impacto económico estimados a partir de la
        capacidad de la planta y el tiempo fuera."""
        falla = (
            mo_models.Falla.objects
            .select_related("proyecto")
            .filter(pk=pk, deleted_at__isnull=True)
            .first()
        )
        if not falla:
            raise NotFound("Falla no encontrada")

        potencia = float(falla.proyecto.potencia_instalada_kwp or 0)
        inicio = datetime(
            falla.fecha_identificacion.year, falla.fecha_identificacion.month,
            falla.fecha_identificacion.day, tzinfo=dominio._COL_TZ,
        )
        if falla.hora_identificacion:
            inicio = inicio.replace(
                hour=falla.hora_identificacion.hour,
                minute=falla.hora_identificacion.minute,
            )
        fin = falla.fecha_resolucion or datetime.now(timezone.utc)
        horas_fuera = max(0, (fin - inicio).total_seconds() / 3600)

        kwh, cop = dominio.estimar_perdida(potencia, horas_fuera)
        # Se persiste solo si no había estimación: una vez calculada, el número
        # se congela para que el histórico no cambie al reconsultarlo.
        if falla.kwh_perdidos_estimado is None:
            falla.kwh_perdidos_estimado = kwh
            falla.impacto_economico_cop = cop
            falla.save(update_fields=["kwh_perdidos_estimado", "impacto_economico_cop"])

        return Response({
            "falla_id": falla.id,
            "proyecto_nombre": falla.proyecto.nombre_comercial,
            "potencia_instalada_kwp": potencia or None,
            "horas_fuera": round(horas_fuera, 1),
            "kwh_perdidos_estimado": kwh,
            "impacto_economico_cop": cop,
        })

    # ── Adjuntos en Google Drive ──────────────────────────────────────────

    @action(detail=True, methods=["get", "post"], url_path="archivos")
    def archivos(self, request, pk=None):
        if request.method == "GET":
            return Response(_fotos_como_objetos(dominio.fotos_lista(self._falla_simple(pk))))
        return self._subir_adjunto(request, pk)

    @action(detail=True, methods=["post"], url_path="attachments")
    def attachments(self, request, pk=None):
        """Alias histórico de `POST /archivos`. Mismo comportamiento."""
        return self._subir_adjunto(request, pk)

    def _subir_adjunto(self, request, pk):
        from googleapiclient.http import MediaIoBaseUpload

        falla = (
            mo_models.Falla.objects
            .select_related("proyecto")
            .filter(pk=pk, deleted_at__isnull=True)
            .first()
        )
        if not falla:
            raise NotFound("Falla no encontrada")

        archivo = request.FILES.get("archivo")
        if archivo is None:
            raise ValidationError("Falta el archivo")
        contenido = archivo.read()
        if len(contenido) > FALLA_MAX_FILE_SIZE:
            raise ValidationError("El archivo supera el límite de 20 MB")

        service = _drive()
        proyecto_nombre = (
            falla.proyecto.nombre_comercial if falla.proyecto_id
            else f"Proyecto {falla.proyecto_id}"
        )
        try:
            # Estructura en Drive: raíz → proyecto → código de falla.
            carpeta_proyecto = _carpeta(service, proyecto_nombre, DRIVE_ROOT_FOLDER_ID)
            carpeta_falla = _carpeta(
                service, falla.codigo_interno or f"FAL-{pk}", carpeta_proyecto
            )
        except Exception as exc:
            raise ServicioNoDisponible(f"Error accediendo carpeta Drive: {exc}")

        nombre = archivo.name or f"archivo_{uuid.uuid4().hex}"
        tipo_mime = archivo.content_type or "application/octet-stream"
        try:
            subido = service.files().create(
                body={"name": nombre, "parents": [carpeta_falla]},
                media_body=MediaIoBaseUpload(io.BytesIO(contenido), mimetype=tipo_mime),
                fields="id, webViewLink",
                supportsAllDrives=True,
            ).execute()
        except Exception as exc:
            raise ServicioNoDisponible(f"Error subiendo archivo a Drive: {exc}")

        file_id = subido["id"]
        nuevo = {
            "id": file_id,
            "nombre": nombre,
            "url": subido.get(
                "webViewLink", f"https://drive.google.com/file/d/{file_id}/view"
            ),
            "tamaño": len(contenido),
            "tipo_mime": tipo_mime,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        items = _fotos_como_objetos(dominio.fotos_lista(falla))
        items.append(nuevo)
        falla.fotos_urls = items
        falla.save(update_fields=["fotos_urls"])
        return Response(nuevo)

    @action(
        detail=True, methods=["delete"],
        url_path=r"archivos/(?P<archivo_id>[\w-]+)",
    )
    def archivo(self, request, pk=None, archivo_id=None):
        falla = self._falla_simple(pk)
        items = _fotos_como_objetos(dominio.fotos_lista(falla))
        restantes = [i for i in items if i.get("id") != archivo_id]
        if len(restantes) == len(items):
            raise NotFound("Archivo no encontrado")

        # Borrarlo de Drive no es crítico: el registro se quita de la base igual,
        # pero queda el log para poder limpiar los huérfanos después.
        try:
            _drive().files().delete(fileId=archivo_id, supportsAllDrives=True).execute()
        except Exception:
            logger.warning(
                "No se pudo borrar de Drive el archivo %s de la falla %s "
                "(queda huérfano en Drive)",
                archivo_id, falla.codigo_interno, exc_info=True,
            )

        falla.fotos_urls = restantes or None
        falla.save(update_fields=["fotos_urls"])
        return Response({"status": "ok"})
