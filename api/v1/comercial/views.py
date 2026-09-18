"""ViewSet del CRM comercial — 24 rutas.

**La unidad es la OFERTA, no el cliente.** Un cliente puede tener varias ofertas
en etapas distintas: la de representación cerrada y la de compra de energía
todavía abierta. Por eso la vista principal (`GET /comercial/ofertas`) es plana
sobre ofertas y la alerta se calcula por oferta.

Toda la lógica vive en `apps/comercial/services/`; acá se valida y se responde.
"""

import json
from pathlib import Path

from django.conf import settings as django_settings
from django.db import IntegrityError, transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response

from api.exceptions import Conflict, NoProcesable
from api.logging import class_logger_wrapper
from api.permissions import RolePermission
from api.v1.cumplimiento import parametros as par
from api.v1.proyectos.serializers import ProyectoDesdeCrmSerializer
from apps.comercial import models as co_models
from apps.comercial.services import (
    actualizacion, consultas, escritura, mantenimiento, pipeline, salidas,
    versiones,
)
from apps.fronteras.models import OperadorRed
from apps.proyectos.models import Proyecto
from apps.proyectos.services.unicidad import (
    buscar_duplicado_por_nombre, verificar_unicos,
)

from . import serializers as co_serializers

ARCHIVO_ACTUALIZACION = "comercial_actualizacion_2026-07.json"


def _ruta_actualizacion() -> Path:
    """`data/comercial_actualizacion_2026-07.json`, desde la raíz del repo."""
    return Path(django_settings.BASE_DIR) / "data" / ARCHIVO_ACTUALIZACION


@class_logger_wrapper(name="Operaciones | Comercial")
class ComercialViewSet(viewsets.GenericViewSet):
    """Pipeline comercial: oportunidades, ofertas y los PPAs que salen de ellas.

    GET  /api/v1/comercial/config
    GET  /api/v1/comercial/oportunidades  ·  POST /api/v1/comercial/oportunidades
    POST /api/v1/comercial/registrar
    GET|PATCH|DELETE /api/v1/comercial/oportunidades/{id}
    POST /api/v1/comercial/oportunidades/{id}/estado
    GET|POST /api/v1/comercial/oportunidades/{id}/gestiones
    POST /api/v1/comercial/oportunidades/{id}/proyectos
    GET|POST /api/v1/comercial/oportunidades/{id}/ofertas
    GET  /api/v1/comercial/ofertas  ·  PATCH|DELETE /api/v1/comercial/ofertas/{id}
    POST /api/v1/comercial/ofertas/{id}/estado · /firmar · /seguimiento
    GET|POST /api/v1/comercial/ofertas/{id}/versiones
    POST /api/v1/comercial/ofertas/{id}/versiones/{numero}/aceptar
    POST /api/v1/comercial/ofertas/vincular-proyectos
    GET  /api/v1/comercial/proyectos-operando
    POST /api/v1/comercial/backfill · /dedup-clientes · /aplicar-actualizacion

    **`/proyectos-operando` no exige rol comercial**: es de solo lectura y no
    expone precios, márgenes ni bitácora. Todo lo demás sí.
    """

    permission_classes = [RolePermission]
    required_role = ["comercial"]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    queryset = co_models.Oportunidad.objects.none()
    lookup_value_regex = r"\d+"

    def _solo_admin(self, request, mensaje: str) -> None:
        if "admin" not in (getattr(request.user, "roles", None) or []):
            raise PermissionDenied(mensaje)

    # ── Configuración y listados ──────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="config")
    def config(self, request):
        return Response({"alerta_dias": salidas.ALERTA_DIAS})

    @action(detail=False, methods=["get", "post"], url_path="oportunidades")
    def oportunidades(self, request):
        if request.method == "GET":
            return Response(consultas.listar_oportunidades(
                estado=request.query_params.get("estado"),
                tipo_servicio=request.query_params.get("tipo_servicio"),
                cliente_id=par.entero(request, "cliente_id"),
                q=request.query_params.get("q"),
                solo_alerta=par.bandera(request, "solo_alerta"),
            ))
        return self._crear_oportunidad(request)

    def _crear_oportunidad(self, request):
        """Crea la oportunidad SOLA, sin ofertas.

        Se conserva para el import y para quien la consuma por API; el registro
        de la UI usa `POST /comercial/registrar`, porque una oportunidad sin
        ofertas no se ve en ninguna vista.
        """
        entrada = co_serializers.OportunidadCrearSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data
        with transaction.atomic():
            cliente = escritura.resolver_cliente(
                datos.get("cliente_id"), datos.get("cliente_nuevo"),
                datos["forzar_cliente_duplicado"],
            )
            op = escritura.nueva_oportunidad(
                cliente, datos.get("nombre"), datos.get("notas"), request.user,
            )
        base = salidas.op_base_out(op, cliente, None, pipeline.col_now())
        return Response(
            base | {"num_proyectos": 0, "capacidad_total_kwp": 0.0},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="registrar")
    def registrar(self, request):
        """Registro comercial completo en UNA transacción: cliente + oportunidad
        + sus ofertas. Es lo que usa el wizard de la UI.

        Todo o nada a propósito: en dos llamadas, cuando la segunda fallaba
        quedaba una oportunidad sin ofertas, la UI decía "creado" y después no
        aparecía en ninguna vista.
        """
        entrada = co_serializers.RegistroComercialSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data

        with transaction.atomic():
            cliente = escritura.resolver_cliente(
                datos.get("cliente_id"), datos.get("cliente_nuevo"),
                datos["forzar_cliente_duplicado"],
            )
            op = escritura.nueva_oportunidad(
                cliente, datos.get("nombre"), datos.get("notas"), request.user,
            )
            ofertas = [
                escritura.nueva_oferta(op.id, dict(o), request.user)
                for o in datos["ofertas"]
            ]

        todas_las_fichas = consultas.fichas(ofertas)
        todas_las_plantas = consultas.plantas_de_ofertas(ofertas)
        base = salidas.op_base_out(
            op, cliente, None, pipeline.col_now(),
            [(salidas.valor(o.estado), o.estado_desde) for o in ofertas],
        )
        return Response(base | {
            "num_proyectos": 0,
            "capacidad_total_kwp": 0.0,
            "ofertas": [
                salidas.oferta_out(o, todas_las_fichas[o.id], todas_las_plantas.get(o.id, []))
                for o in ofertas
            ],
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"], url_path="ofertas")
    def ofertas(self, request):
        return Response(consultas.listar_ofertas(
            tipo=request.query_params.get("tipo"),
            estado=request.query_params.get("estado"),
            resultado=request.query_params.get("resultado"),
            q=request.query_params.get("q"),
            solo_alerta=par.bandera(request, "solo_alerta"),
        ))

    @action(detail=False, methods=["get"], url_path="proyectos-operando")
    def proyectos_operando(self, request):
        """Los CONTRATOS DE ENERGÍA del pipeline: un árbol PPA → proyectos → ficha.

        **Un PPA no firmado no existe como contrato.** La oferta del CRM *es* el
        PPA hasta que se firma, y `ppa.id` lo dice sin ambigüedad: en `null` no
        hay fila en `ppa_contratos` (condiciones tentativas de la oferta), con
        valor el contrato existe y aparece en /servicios.

        `estado: firmado|operando` con `ppa.id: null` es una **inconsistencia**:
        el negocio cerró y el PPA no está cargado. No se rellena inventando un
        contrato de campos nulos —eso metería compromisos fantasma en
        Cumplimiento—; se deja ver para que se cargue.
        """
        pedidas = request.query_params.getlist("estado_pipeline")
        etapas = tuple(pedidas) if pedidas else pipeline.ETAPAS_CON_PPA
        invalidas = [e for e in etapas if e not in pipeline.ETAPAS_CON_PPA]
        if invalidas:
            # 422 explícito y no una lista vacía: pedir una etapa que no produce
            # PPAs y recibir 200 con cero filas se lee como "no hay ninguno", que
            # es otra cosa.
            raise NoProcesable(
                f"Etapa no válida: {', '.join(invalidas)}. "
                f"Las etapas que producen un PPA son: "
                f"{', '.join(pipeline.ETAPAS_CON_PPA)}."
            )

        nodos = pipeline.ppas_del_pipeline(
            q=request.query_params.get("q"), estados=etapas,
        )
        por_estado: dict[str, int] = {}
        for n in nodos:
            clave = n["ppa"]["estado"]
            por_estado[clave] = por_estado.get(clave, 0) + 1
        return Response({
            # `ahora_colombia()` y no `col_now()`: esta fecha viaja hacia afuera y
            # tiene que traer su offset real (−05:00).
            "generado_en": pipeline.ahora_colombia(),
            "estados_pipeline": list(etapas),
            "total": len(nodos),
            # Solo los estados presentes: un cero explícito de un estado que no se
            # pidió es ruido.
            "por_estado": por_estado,
            "ppas": nodos,
        })

    def get_permissions(self):
        # `/proyectos-operando` es de integración y no expone nada comercial.
        if self.action == "proyectos_operando":
            return [RolePermission()]
        return super().get_permissions()

    # ── Detalle de la oportunidad ─────────────────────────────────────────

    @action(
        detail=False, methods=["get", "patch", "delete"],
        url_path=r"oportunidades/(?P<id>\d+)",
    )
    def oportunidad(self, request, id=None):
        if request.method == "GET":
            return self._detalle_oportunidad(int(id))
        if request.method == "PATCH":
            op = escritura.get_oportunidad(int(id))
            entrada = co_serializers.OportunidadActualizarSerializer(
                data=request.data, partial=True,
            )
            entrada.is_valid(raise_exception=True)
            for campo, v in entrada.validated_data.items():
                setattr(op, campo, v)
            op.save()
            return Response({"ok": True, "id": op.id})

        self._solo_admin(request, "Solo admin puede eliminar oportunidades")
        op = escritura.get_oportunidad(int(id))
        # Los proyectos NO se borran: quedan vinculados a las ofertas por la M2M,
        # pero como todas las lecturas filtran `deleted_at IS NULL`, dejan de
        # aparecer en cuanto la oportunidad se marca borrada.
        op.deleted_at = pipeline.col_now()
        op.save(update_fields=["deleted_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _detalle_oportunidad(self, id: int):
        op = (
            co_models.Oportunidad.objects
            .select_related("cliente")
            .prefetch_related("gestiones", "ofertas", "cliente__contactos")
            .filter(pk=id, deleted_at__isnull=True)
            .first()
        )
        if not op:
            raise NotFound("Oportunidad no encontrada")

        ofertas = list(op.ofertas.all())
        gestiones = list(op.gestiones.all())
        historial = list(
            co_models.OportunidadEstadoHistorial.objects.filter(oportunidad_id=op.id)
        )
        documentos = list(op.cliente.documentos_comerciales.all())

        base = salidas.op_base_out(
            op, op.cliente, gestiones[0].fecha if gestiones else None,
            pipeline.col_now(),
            [(salidas.valor(o.estado), o.estado_desde) for o in ofertas],
        )
        todas_las_fichas = consultas.fichas(ofertas)
        todas_las_plantas = consultas.plantas_de_ofertas(ofertas)
        base.update({
            "notas": op.notas,
            "fecha_tentativa_inicio_representacion": op.fecha_tentativa_inicio_representacion,
            "fecha_tentativa_inicio_compra_energia": op.fecha_tentativa_inicio_compra_energia,
            "fecha_estimada_firma": op.fecha_estimada_firma,
            "cliente": {
                "id": op.cliente.id,
                "razon_social_nombre": op.cliente.razon_social_nombre,
                "nit_cedula": op.cliente.nit_cedula,
                "origen_tipo": op.cliente.origen_tipo,
                "origen_detalle": op.cliente.origen_detalle,
            },
            # La unión de las plantas de todas sus ofertas, deduplicadas por id.
            # Antes salía de `Proyecto.oportunidad_id`, una columna que nunca se
            # llenaba (0/188): esta sección siempre decía "Sin proyectos
            # vinculados" aunque la oportunidad sí tuviera plantas reales.
            "proyectos": list(
                {p["id"]: p for lista in todas_las_plantas.values() for p in lista}.values()
            ),
            "documentos": [
                {"id": d.id, "tipo": d.tipo, "nombre": d.nombre, "numero": d.numero,
                 "estado": d.estado, "archivo_url": d.archivo_url,
                 "archivo_nombre": d.archivo_nombre, "fecha": d.fecha}
                for d in documentos
            ],
            "gestiones": [
                {"id": g.id, "tipo": g.tipo, "descripcion": g.descripcion,
                 "fecha": g.fecha, "usuario_id": g.usuario_id, "oferta_id": g.oferta_id}
                for g in gestiones
            ],
            "historial": [
                {"id": h.id, "estado_anterior": h.estado_anterior,
                 "estado_nuevo": h.estado_nuevo, "fecha": h.created_at,
                 "usuario_id": h.usuario_id, "oferta_id": h.oferta_id}
                for h in historial
            ],
            "ofertas": [
                salidas.oferta_out(o, todas_las_fichas[o.id], todas_las_plantas.get(o.id, []))
                for o in ofertas
            ],
            "resumen_ofertas": salidas.resumen_ofertas(ofertas),
        })
        return Response(base)

    @action(detail=False, methods=["post"], url_path=r"oportunidades/(?P<id>\d+)/estado")
    def oportunidad_estado(self, request, id=None):
        """Mueve TODAS las ofertas del cliente a una etapa.

        Se conserva porque el tablero viejo arrastra la tarjeta del cliente; para
        mover una sola oferta —que es lo normal— está `/ofertas/{id}/estado`.
        """
        op = escritura.get_oportunidad(int(id))
        entrada = co_serializers.EstadoCambioSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        return Response(escritura.mover_todas_las_ofertas(
            op, entrada.validated_data["estado"], request.user,
        ))

    @action(
        detail=False, methods=["get", "post"],
        url_path=r"oportunidades/(?P<id>\d+)/gestiones",
    )
    def gestiones(self, request, id=None):
        escritura.get_oportunidad(int(id))
        if request.method == "GET":
            return Response([
                {"id": g.id, "tipo": g.tipo, "descripcion": g.descripcion,
                 "fecha": g.fecha, "usuario_id": g.usuario_id, "oferta_id": g.oferta_id}
                for g in co_models.OportunidadGestion.objects
                .filter(oportunidad_id=id).order_by("-fecha")
            ])

        entrada = co_serializers.GestionCrearSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data
        # Una gestión puede ser DE UNA OFERTA (apaga solo su alerta) o del
        # cliente (`oferta_id` NULL: cuenta para todas). Se valida la pertenencia
        # para que no se pueda colgar la llamada de un cliente en la oferta de otro.
        if datos.get("oferta_id") is not None:
            if not co_models.OportunidadOferta.objects.filter(
                pk=datos["oferta_id"], oportunidad_id=id,
            ).exists():
                raise NoProcesable("La oferta no pertenece a esta oportunidad")

        g = co_models.OportunidadGestion.objects.create(
            oportunidad_id=id, oferta_id=datos.get("oferta_id"), tipo=datos["tipo"],
            descripcion=datos["descripcion"],
            fecha=datos.get("fecha") or pipeline.col_now(),
            usuario_id=request.user.id,
        )
        return Response({
            "id": g.id, "tipo": g.tipo, "descripcion": g.descripcion,
            "fecha": g.fecha, "oferta_id": g.oferta_id,
        }, status=status.HTTP_201_CREATED)

    @action(
        detail=False, methods=["post"],
        url_path=r"oportunidades/(?P<id>\d+)/proyectos",
    )
    def oportunidad_proyectos(self, request, id=None):
        """Crea una planta desde el CRM. **Es un Proyecto normal**: misma tabla,
        mismo esquema y las mismas validaciones que `POST /proyectos`.

        Con `oferta_id` la planta queda vinculada a esa oferta, que es lo que hace
        que aparezca en `/comercial/proyectos-operando`: esa API resuelve las
        plantas por la M2M de la oferta, no por una columna del proyecto.
        """
        escritura.get_oportunidad(int(id))
        entrada = ProyectoDesdeCrmSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        payload = dict(entrada.validated_data)

        if not OperadorRed.objects.filter(pk=payload.get("operador_red_id")).exists():
            raise NoProcesable(
                "Debes seleccionar un operador de red válido del catálogo"
            )

        oferta = None
        oferta_id = par.entero(request, "oferta_id")
        if oferta_id is not None:
            oferta = co_models.OportunidadOferta.objects.filter(
                pk=oferta_id, oportunidad_id=id,
            ).first()
            if not oferta:
                raise NoProcesable(f"La oferta {oferta_id} no es de esta oportunidad")

        # Las MISMAS dos validaciones del POST /proyectos general. Se reusan y no
        # se reimplementan para que crear desde el CRM y crear desde /proyectos no
        # puedan divergir.
        verificar_unicos(payload)
        if not par.bandera(request, "forzar"):
            duplicado = buscar_duplicado_por_nombre(
                payload.get("nombre_comercial"), payload.get("tipo_proyecto"),
            )
            if duplicado:
                raise Conflict({
                    "codigo": "posible_duplicado",
                    "mensaje": (
                        f"Ya existe un proyecto con nombre parecido: "
                        f"{duplicado.nombre_comercial}"
                    ),
                    "proyecto_id": duplicado.id,
                    # Mismas claves que el 409 de POST /proyectos, para que un
                    # cliente que ya lo maneje no tenga que aprender otra forma.
                    "duplicado_nombre": True,
                    "candidato_id": duplicado.id,
                    "candidato_nombre": duplicado.nombre_comercial,
                })

        # El CRM manda esto, no el cliente: es de la creación, no del formulario.
        payload["origen"] = "manual"
        try:
            with transaction.atomic():
                proyecto = Proyecto.objects.create(**payload)
                if oferta is not None:
                    escritura.sumar_planta(oferta, proyecto.id)
        except IntegrityError:
            raise Conflict(
                "No se pudo guardar: algún valor único (p. ej. API ID Unergy, "
                "topic slug, ID de Solenium o de Sun Factory) ya está en uso por "
                "otro proyecto."
            )
        return Response(
            salidas.proyecto_out(
                Proyecto.objects.select_related("operador_red")
                .prefetch_related("fronteras__operador_red").get(pk=proyecto.id)
            ),
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=False, methods=["get", "post"],
        url_path=r"oportunidades/(?P<id>\d+)/ofertas",
    )
    def oportunidad_ofertas(self, request, id=None):
        escritura.get_oportunidad(int(id))
        if request.method == "GET":
            ofertas = list(
                co_models.OportunidadOferta.objects
                .filter(oportunidad_id=id).order_by("id")
            )
            todas_las_fichas = consultas.fichas(ofertas)
            todas_las_plantas = consultas.plantas_de_ofertas(ofertas)
            return Response([
                salidas.oferta_out(o, todas_las_fichas[o.id], todas_las_plantas.get(o.id, []))
                for o in ofertas
            ])

        entrada = co_serializers.OfertaCrearSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        with transaction.atomic():
            oferta = escritura.nueva_oferta(int(id), dict(entrada.validated_data), request.user)
        return Response(
            consultas.oferta_completa(oferta), status=status.HTTP_201_CREATED,
        )

    # ── Ofertas ───────────────────────────────────────────────────────────

    def _oferta(self, oferta_id) -> co_models.OportunidadOferta:
        oferta = co_models.OportunidadOferta.objects.filter(pk=oferta_id).first()
        if not oferta:
            raise NotFound("Oferta no encontrada")
        return oferta

    @action(
        detail=False, methods=["patch", "delete"],
        url_path=r"ofertas/(?P<oferta_id>\d+)",
    )
    def oferta(self, request, oferta_id=None):
        oferta = self._oferta(oferta_id)
        if request.method == "DELETE":
            oferta.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        entrada = co_serializers.OfertaActualizarSerializer(data=request.data, partial=True)
        entrada.is_valid(raise_exception=True)
        cambios = dict(entrada.validated_data)
        if "operador_red_id" in cambios:
            escritura.validar_operador_red(cambios["operador_red_id"])
        # La M2M se escribe aparte y ella misma sincroniza `proyecto_id`, así que
        # se aplica DESPUÉS de los setattr para que no la pise un `proyecto_id`
        # explícito.
        proyecto_ids = cambios.pop("proyecto_ids", None)
        with transaction.atomic():
            for campo, v in cambios.items():
                setattr(oferta, campo, v)
            if proyecto_ids is not None:
                escritura.set_plantas(oferta, proyecto_ids)
            oferta.save()
        # Devuelve la fila COMPLETA para que el autosave del drawer refresque la
        # ficha resuelta sin recargar la lista entera.
        return Response(consultas.oferta_completa(oferta))

    @action(detail=False, methods=["post"], url_path=r"ofertas/(?P<oferta_id>\d+)/estado")
    def oferta_estado(self, request, oferta_id=None):
        """Mueve UNA oferta de etapa: una se firma sin arrastrar a sus hermanas."""
        oferta = self._oferta(oferta_id)
        entrada = co_serializers.EstadoCambioSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        escritura.mover_oferta(oferta, entrada.validated_data["estado"], request.user)
        return Response(consultas.oferta_completa(oferta))

    @action(detail=False, methods=["post"], url_path=r"ofertas/(?P<oferta_id>\d+)/firmar")
    def oferta_firmar(self, request, oferta_id=None):
        """La oferta evoluciona en su contrato PPA y queda 'firmada'."""
        oferta = self._oferta(oferta_id)
        entrada = co_serializers.FirmarOfertaSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = dict(entrada.validated_data)
        resultado = escritura.firmar(oferta, datos, request.user)
        return Response({
            "oferta": consultas.oferta_completa(oferta),
            "ppa_contrato_id": resultado.contrato.id,
            # Los conteos salen del resultado y no se recalculan: antes
            # `tarifas_creadas` volvía a expandir la tabla de precios acá, y dos
            # cuentas de lo mismo pueden dejar de coincidir.
            "tarifas_creadas": resultado.tarifas,
            # Firmar con 0 plantas es legítimo —la planta puede no existir todavía
            # como Proyecto— pero Cumplimiento no puede medir ese PPA, así que el
            # dato viaja para que la UI avise en vez de dejarlo pasar en silencio.
            "plantas_del_contrato": resultado.plantas,
            "avisos": resultado.avisos,
        }, status=status.HTTP_201_CREATED)

    @action(
        detail=False, methods=["post"],
        url_path=r"ofertas/(?P<oferta_id>\d+)/seguimiento",
    )
    def oferta_seguimiento(self, request, oferta_id=None):
        """Un click: suma un toque a la oferta. NO toca `fecha_oferta` — el toque
        de hoy no es el primer envío."""
        oferta = self._oferta(oferta_id)
        oferta.seguimientos = (oferta.seguimientos or 0) + 1
        oferta.save(update_fields=["seguimientos"])
        return Response(consultas.oferta_completa(oferta))

    # ── Versiones de la oferta ────────────────────────────────────────────

    @action(
        detail=False, methods=["get", "post"],
        url_path=r"ofertas/(?P<oferta_id>\d+)/versiones",
    )
    def oferta_versiones(self, request, oferta_id=None):
        """Las propuestas de una oferta. Reofertar es agregar una, no editar.

        `GET` las devuelve de la más nueva a la más vieja: la que interesa es la
        última. `POST` agrega la siguiente y le asigna el número; sin
        `fecha_envio` nace como borrador.

        **No hay `PATCH` ni `DELETE` a propósito.** Son append-only: de eso
        depende que la cronología sea confiable, igual que en la bitácora de
        Prospección. Ver `docs/DOMINIO_COMERCIAL.md`, O-8 y O-9.
        """
        oferta = self._oferta(oferta_id)

        if request.method == "GET":
            filas = (
                oferta.versiones.prefetch_related("precios").order_by("-numero")
            )
            return Response(
                co_serializers.VersionOfertaSerializer(filas, many=True).data
            )

        entrada = co_serializers.VersionOfertaCrearSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        version = versiones.agregar(oferta, dict(entrada.validated_data), request.user)
        return Response(
            co_serializers.VersionOfertaSerializer(version).data,
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=False, methods=["post"],
        url_path=r"ofertas/(?P<oferta_id>\d+)/versiones/(?P<numero>\d+)/aceptar",
    )
    def aceptar_version(self, request, oferta_id=None, numero=None):
        """Marca cuál propuesta aceptó el cliente. Es la que se firmará.

        De acá saldrán las condiciones con las que nace el PPA, así que como
        mucho puede haber una aceptada por oferta: dos harían ambiguo con qué
        precio se firma.
        """
        oferta = self._oferta(oferta_id)
        version = oferta.versiones.filter(numero=numero).first()
        if version is None:
            raise NotFound(f"La oferta no tiene una versión {numero}")

        entrada = co_serializers.AceptarVersionSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        versiones.aceptar(version, entrada.validated_data["fecha_aceptacion"])
        return Response(co_serializers.VersionOfertaSerializer(version).data)

    @action(detail=False, methods=["post"], url_path="ofertas/vincular-proyectos")
    def vincular_proyectos(self, request):
        """Vincula por nombre las ofertas del CRM con las plantas ya cargadas.

        **Por defecto no escribe** (`dry_run=true`): devuelve `propuestos`,
        `sin_candidato` (con el mejor puntaje, para saber si faltó poco o no hay
        nada parecido) y `sin_nombre`. Revisá esa lista antes de correrlo con
        `dry_run=false`.
        """
        self._solo_admin(request, "Solo admin puede vincular ofertas a proyectos")
        pedidos = request.query_params.getlist("estado")
        etapas = (
            None if par.bandera(request, "todas_las_etapas")
            else (tuple(pedidos) if pedidos else pipeline.ETAPAS_ENTREGABLES)
        )
        umbral = float(request.query_params.get("umbral") or pipeline.UMBRAL_VINCULO)
        if not (0.5 <= umbral <= 1.0):
            raise NoProcesable("'umbral' debe estar entre 0.5 y 1.0")
        ofertas = [int(v) for v in request.query_params.getlist("oferta_id")] or None
        return Response(pipeline.vincular_proyectos(
            estados=etapas, umbral=umbral,
            dry_run=par.bandera(request, "dry_run", defecto=True),
            solo_ofertas=ofertas,
        ))

    # ── Mantenimiento (solo admin) ────────────────────────────────────────

    @action(detail=False, methods=["post"], url_path="backfill")
    def backfill(self, request):
        """Una oportunidad 'operando' por cliente existente sin oportunidad,
        vinculando sus proyectos. Idempotente."""
        self._solo_admin(request, "Solo admin")
        return Response(mantenimiento.ejecutar_backfill(
            request.user.id,
            dry_run=par.bandera(request, "dry_run", defecto=True),
            solo_con_relacion_comercial=par.bandera(
                request, "solo_con_relacion_comercial"
            ),
        ))

    @action(detail=False, methods=["post"], url_path="dedup-clientes")
    def dedup_clientes(self, request):
        """Limpia los clientes-prospecto duplicados del import. Reversible: hace
        soft-delete, no borra."""
        self._solo_admin(request, "Solo admin")
        umbral = float(request.query_params.get("umbral") or 0.85)
        return Response(mantenimiento.dedup_clientes(
            request.user.id,
            dry_run=par.bandera(request, "dry_run", defecto=True),
            umbral=umbral,
        ))

    @action(detail=False, methods=["post"], url_path="aplicar-actualizacion")
    def aplicar_actualizacion(self, request):
        """Aplica `data/comercial_actualizacion_2026-07.json`. `dry_run` por
        defecto: devuelve el reporte sin escribir nada."""
        self._solo_admin(request, "Solo admin")
        ruta = _ruta_actualizacion()
        if not ruta.exists():
            raise NotFound("Archivo de actualización no encontrado")
        datos = json.loads(ruta.read_text(encoding="utf-8"))

        problemas = actualizacion.validar(datos)
        dry_run = par.bandera(request, "dry_run", defecto=True)
        if problemas and not dry_run:
            raise NoProcesable({"problemas_del_archivo": problemas})

        with transaction.atomic():
            reporte = actualizacion.aplicar(datos, dry_run=dry_run)
        reporte["problemas_del_archivo"] = problemas
        reporte["ya_aplicado"] = actualizacion.ya_aplicado()
        return Response(reporte)
