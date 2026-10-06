"""ViewSet de informes guardados y su flujo de aprobación."""

from datetime import datetime, timezone

from django.db import connection, transaction
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import (
    NotFound, PermissionDenied, ValidationError,
)
from rest_framework.response import Response

from api.exceptions import Conflict
from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.clientes.services import contactos as contactos_service
from apps.plataforma import models as pl_models
from apps.plataforma.services import informes as informes_service
from apps.proyectos import models as py_models

from . import serializers as inf_serializers

LIMITE_MAXIMO = 500


def _entero(request, nombre, defecto, maximo):
    crudo = request.query_params.get(nombre)
    if crudo in (None, ""):
        return defecto
    if not crudo.isdigit() or not 1 <= int(crudo) <= maximo:
        raise ValidationError({nombre: f"Entero entre 1 y {maximo}."})
    return int(crudo)


@class_logger_wrapper(name="Operaciones | Plataforma | Informes")
class InformeViewSet(viewsets.GenericViewSet):
    """Informes generados en Monitoreo, con su flujo editorial.

    POST   /api/v1/informes/                    upsert por tipo+proyecto+período
    GET    /api/v1/informes/[?tipo=&sub_project=&estado=&…&limit=]
    GET    /api/v1/informes/envios              histórico de correos enviados
    GET    /api/v1/informes/{id}[/compuesto]
    PATCH  /api/v1/informes/{id}/seccion        write-back desde el portafolio
    PATCH  /api/v1/informes/{id}/estado
    POST   /api/v1/informes/{id}/comentarios
    PATCH  /api/v1/informes/{id}/comentarios/{cid}/resolver
    DELETE /api/v1/informes/{id}/comentarios/{cid}
    DELETE /api/v1/informes/{id}
    POST   /api/v1/informes/{id}/enviar

    **El flujo es una máquina de estados**: `borrador → revisado → aprobado`.
    Solo el verificador aprueba o reabre, y un informe con comentarios sin
    subsanar no se puede aprobar. Ver `apps/plataforma/services/informes.py`.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    queryset = pl_models.InformeGuardado.objects.all()

    def _informe(self, pk):
        informe = pl_models.InformeGuardado.objects.filter(pk=pk).first()
        if informe is None:
            raise NotFound("Informe no encontrado")
        return informe

    @property
    def _usuario(self):
        return self.request.user.usuario

    # ── Listado y upsert ──────────────────────────────────────────────────

    def list(self, request, *args, **kwargs):
        consulta = pl_models.InformeGuardado.objects.all()
        for parametro in ("tipo", "sub_project", "estado"):
            valor = request.query_params.get(parametro)
            if valor:
                consulta = consulta.filter(**{parametro: valor})
        for parametro, filtro in (
            ("periodo_desde_gte", "periodo_desde__gte"),
            ("periodo_desde_lte", "periodo_desde__lte"),
        ):
            valor = request.query_params.get(parametro)
            if valor:
                consulta = consulta.filter(**{filtro: valor})

        limite = _entero(request, "limit", 50, LIMITE_MAXIMO)
        filas = consulta.order_by("-editado_en")[:limite]
        return Response(
            inf_serializers.InformeSerializer(filas, many=True).data
        )

    def create(self, request, *args, **kwargs):
        """Upsert por (tipo, sub_project, período).

        **Solo un borrador se puede sobrescribir.** Un informe en «revisado» o
        «aprobado» está bloqueado para no perder el avance del flujo editorial:
        hay que reabrirlo antes.
        """
        entrada = inf_serializers.UpsertSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data
        ahora = datetime.now(timezone.utc)

        existente = pl_models.InformeGuardado.objects.filter(
            tipo=datos["tipo"], sub_project=datos["sub_project"],
            periodo_desde=datos["periodo_desde"],
            periodo_hasta=datos["periodo_hasta"],
        ).first()

        if existente is None:
            informe = pl_models.InformeGuardado.objects.create(
                **{k: datos.get(k) for k in (
                    "tipo", "sub_project", "periodo_desde", "periodo_hasta",
                    "periodo_display", "proyecto_nombre", "html_content",
                    "charts_data", "miembros",
                )},
                estado="borrador",
                creado_por_id=self._usuario.id,
                creado_por_nombre=self._usuario.nombre,
                editado_por_id=self._usuario.id,
                editado_por_nombre=self._usuario.nombre,
                editado_en=ahora,
            )
            return Response(
                inf_serializers.InformeDetalleSerializer(informe).data
            )

        if existente.estado == "aprobado":
            raise ValidationError("No se puede editar un informe ya aprobado")
        if existente.estado == "revisado":
            raise Conflict(
                "Este informe ya está en estado 'revisado'. Reviértelo a "
                "borrador antes de guardar una nueva versión."
            )

        existente.html_content = datos["html_content"]
        existente.charts_data = datos.get("charts_data")
        if datos.get("miembros") is not None:
            existente.miembros = datos["miembros"]
        if datos.get("proyecto_nombre"):
            existente.proyecto_nombre = datos["proyecto_nombre"]
        if datos.get("periodo_display"):
            existente.periodo_display = datos["periodo_display"]
        existente.editado_por_id = self._usuario.id
        existente.editado_por_nombre = self._usuario.nombre
        existente.editado_en = ahora
        existente.save()
        return Response(
            inf_serializers.InformeDetalleSerializer(existente).data
        )

    def retrieve(self, request, *args, **kwargs):
        return Response(inf_serializers.InformeDetalleSerializer(
            self._informe(kwargs["pk"])
        ).data)

    def destroy(self, request, *args, **kwargs):
        informe = self._informe(kwargs["pk"])
        if informe.estado == "aprobado":
            raise ValidationError("No se puede eliminar un informe aprobado")
        with transaction.atomic():
            informes_service.congelar_en_portafolios(informe)
            informe.delete()
        return Response(status=204)

    # ── Envíos de correo ──────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="envios")
    def envios(self, request):
        """Histórico de correos enviados.

        SQL crudo: `email_envios` es una de las tablas sin modelo (ver
        `apps/README.md`). Cuando se declare, esto pasa a ORM.
        """
        limite = _entero(request, "limit", 50, LIMITE_MAXIMO)
        tipo = request.query_params.get("tipo")
        condicion = "WHERE tipo = %(tipo)s" if tipo else ""
        with connection.cursor() as cursor:
            cursor.execute(f"""
                SELECT id, asunto, tipo, exitoso, error, enviado_at,
                       proyectos, proyectos_total
                FROM email_envios
                {condicion}
                ORDER BY enviado_at DESC LIMIT %(limite)s
            """, {"tipo": tipo, "limite": limite})
            columnas = [c[0] for c in cursor.description]
            return Response([
                dict(zip(columnas, fila)) for fila in cursor.fetchall()
            ])

    # ── Composición y write-back ──────────────────────────────────────────

    @action(detail=True, methods=["get"], url_path="compuesto")
    def compuesto(self, request, pk=None):
        """El HTML listo para previsualizar, imprimir o enviar."""
        informe = self._informe(pk)
        return Response({
            "id": informe.id,
            "tipo": informe.tipo,
            "html_content": informes_service.html_para_enviar(informe),
        })

    @action(detail=True, methods=["patch"], url_path="seccion")
    @log_endpoint(name="Operaciones | Plataforma | Informes | Sección")
    def seccion(self, request, pk=None):
        """Guarda una sección editada DENTRO del portafolio.

        Edición bidireccional: si esa sección corresponde a un informe
        individual editable se escribe ahí —y queda actualizada en las dos
        vistas—; si el proyecto no tiene individual, se guarda en el
        `html_inline` del miembro.
        """
        informe = self._informe(pk)
        if informe.tipo != "port":
            raise ValidationError("Sólo aplica a informes de portafolio")

        entrada = inf_serializers.SeccionSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data
        ahora = datetime.now(timezone.utc)

        individual = informes_service.individual_de(
            datos["sub_project"], informe.periodo_desde, informe.periodo_hasta
        )
        if individual is not None:
            if individual.estado != "borrador":
                raise Conflict(
                    f'El informe individual de "{datos["sub_project"]}" está '
                    f"en estado '{individual.estado}' y no se puede editar "
                    "desde el portafolio. Reábrelo primero."
                )
            individual.html_content = datos["html_content"]
            individual.editado_por_id = self._usuario.id
            individual.editado_por_nombre = self._usuario.nombre
            individual.editado_en = ahora
            individual.save(update_fields=[
                "html_content", "editado_por", "editado_por_nombre",
                "editado_en",
            ])
        else:
            miembros = list(informe.miembros or [])
            objetivo = next(
                (
                    m for m in miembros
                    if isinstance(m, dict)
                    and m.get("sub_project") == datos["sub_project"]
                ),
                None,
            )
            if objetivo is None:
                objetivo = {
                    "sub_project": datos["sub_project"],
                    "nombre": datos["sub_project"],
                    "orden": len(miembros),
                }
                miembros.append(objetivo)
            objetivo["html_inline"] = datos["html_content"]
            informe.miembros = miembros
            informe.save(update_fields=["miembros"])

        informe.refresh_from_db()
        return Response(
            inf_serializers.InformeDetalleSerializer(informe).data
        )

    # ── Estado y comentarios ──────────────────────────────────────────────

    @action(detail=True, methods=["patch"], url_path="estado")
    @log_endpoint(name="Operaciones | Plataforma | Informes | Estado")
    def estado(self, request, pk=None):
        informe = self._informe(pk)
        entrada = inf_serializers.EstadoSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        self._ejecutar(
            informes_service.cambiar_estado,
            informe, entrada.validated_data["estado"], self._usuario,
        )
        return Response(inf_serializers.InformeSerializer(informe).data)

    @action(detail=True, methods=["post"], url_path="comentarios")
    @log_endpoint(name="Operaciones | Plataforma | Informes | Comentario")
    def comentarios(self, request, pk=None):
        informe = self._informe(pk)
        entrada = inf_serializers.ComentarioCrearSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        self._ejecutar(
            informes_service.agregar_comentario,
            informe, entrada.validated_data["mensaje"], self._usuario,
        )
        return Response(
            inf_serializers.InformeDetalleSerializer(informe).data
        )

    @action(
        detail=True, methods=["patch"],
        url_path=r"comentarios/(?P<comentario_id>[^/]+)/resolver",
    )
    @log_endpoint(name="Operaciones | Plataforma | Informes | Resolver")
    def resolver_comentario(self, request, pk=None, comentario_id=None):
        informe = self._informe(pk)
        entrada = inf_serializers.ComentarioResolverSerializer(
            data=request.data
        )
        entrada.is_valid(raise_exception=True)
        self._ejecutar(
            informes_service.resolver_comentario,
            informe, comentario_id,
            entrada.validated_data.get("respuesta"), self._usuario,
        )
        return Response(
            inf_serializers.InformeDetalleSerializer(informe).data
        )

    @action(
        detail=True, methods=["delete"],
        url_path=r"comentarios/(?P<comentario_id>[^/]+)",
    )
    @log_endpoint(name="Operaciones | Plataforma | Informes | Borrar comentario")
    def borrar_comentario(self, request, pk=None, comentario_id=None):
        informe = self._informe(pk)
        self._ejecutar(
            informes_service.borrar_comentario,
            informe, comentario_id, self._usuario,
        )
        return Response(
            inf_serializers.InformeDetalleSerializer(informe).data
        )

    @staticmethod
    def _ejecutar(funcion, *args):
        """Traduce los errores del flujo editorial a sus códigos HTTP."""
        try:
            funcion(*args)
        except informes_service.TransicionInvalida as exc:
            raise ValidationError(str(exc))
        except informes_service.SinPermiso as exc:
            raise PermissionDenied(str(exc))
        except informes_service.Conflicto as exc:
            raise Conflict(str(exc))
        except LookupError as exc:
            raise NotFound(str(exc))
        except ValueError as exc:
            raise ValidationError(str(exc))

    # ── Envío por correo ──────────────────────────────────────────────────

    @action(detail=True, methods=["post"], url_path="enviar")
    @log_endpoint(name="Operaciones | Plataforma | Informes | Enviar")
    def enviar(self, request, pk=None):
        informe = self._informe(pk)
        if informe.estado != "aprobado":
            raise ValidationError(
                "Solo se pueden enviar informes aprobados (verificados)"
            )
        if not informes_service.es_remitente(self._usuario):
            raise PermissionDenied(
                "Sólo Laura H. (o el verificador/admin) puede disparar el "
                "envío del informe por correo."
            )

        proyecto_id = (
            py_models.Proyecto.objects
            .filter(sub_project=informe.sub_project)
            .values_list("id", flat=True).first()
            or py_models.Proyecto.objects
            .filter(nombre_comercial=informe.sub_project)
            .values_list("id", flat=True).first()
        )
        destinatarios = (
            contactos_service.correos("operacional", proyecto_id=proyecto_id)
            if proyecto_id else []
        )
        if not destinatarios:
            return Response({"detail": (
                "No se encontró un contacto operacional para este proyecto. "
                "Configúralo en la ficha del cliente o del proyecto "
                "(tab Contactos)."
            )}, status=422)

        from apps.comun.integraciones.email_service import send_informe_email

        try:
            send_informe_email(
                to_emails=destinatarios,
                proyecto_nombre=informe.proyecto_nombre or informe.sub_project,
                periodo_display=(
                    informe.periodo_display
                    or f"{informe.periodo_desde} — {informe.periodo_hasta}"
                ),
                aprobado_por=(
                    informe.aprobado_por_nombre or self._usuario.nombre
                ),
                html_content=informes_service.html_para_enviar(informe),
                proyecto_id=proyecto_id,
            )
        except RuntimeError as exc:
            return Response({"detail": str(exc)}, status=503)
        except Exception as exc:
            return Response(
                {"detail": f"Error al enviar email: {exc}"}, status=500
            )

        informe.correo_enviado = True
        informe.correo_enviado_en = datetime.now(timezone.utc)
        informe.enviado_por_id = self._usuario.id
        informe.enviado_por_nombre = self._usuario.nombre
        informe.save(update_fields=[
            "correo_enviado", "correo_enviado_en", "enviado_por",
            "enviado_por_nombre",
        ])
        return Response({"ok": True, "enviado_a": destinatarios})
