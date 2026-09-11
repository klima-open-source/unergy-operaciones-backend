"""ViewSet de Proyectos — 29 rutas.

El proyecto es la entidad más referenciada del repo: fallas, cumplimiento,
liquidaciones, comercial y monitoreo cuelgan de él. Por eso sus helpers
compartidos (`unicidad`, `resolucion`, `tsf_sync`, el serializer de creación) no
viven acá sino en `apps/proyectos/services/` y `serializers.py`, de donde los
consumen los otros recursos.

`/pendientes`, `/gen-promedio`, `/lista` y `/buscar` van declaradas ANTES del
detalle. En FastAPI hacía falta porque `{id}` está tipado `int` y "lista" no
convierte; acá `lookup_value_regex` ya acota a dígitos, pero el orden se
conserva por consistencia y porque el router las resuelve primero igual.
"""

from datetime import datetime, timezone

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from api.exceptions import Conflict, ServicioNoDisponible
from api.logging import class_logger_wrapper
from api.pagination import PaginacionConPaginas
from api.permissions import RolePermission
from api.v1.cumplimiento import parametros as par
from apps.clientes.models import Cliente, ProyectoAreaContacto
from apps.fronteras.models import Frontera
from apps.ppa.models import PpaContratoProyecto
from apps.proyectos import models as py_models
from apps.proyectos.services import gen_promedio, gestion, pendientes as pendientes_svc
from apps.proyectos.services.operador_red import sincronizar_operador_red
from apps.proyectos.services.resolucion import id_por_nombre
from apps.proyectos.services.unicidad import (
    buscar_duplicado_por_nombre, verificar_unicos,
)

from . import serializers as py_serializers

# Los flags de servicio, por si el listado quiere filtrar por uno. Los mismos
# que acepta el PATCH de /proyectos/{id}/servicios.
SERVICIOS = (
    "operacion", "representacion", "cgm", "ppa",
)


def _con_relaciones():
    """El detalle completo con todo lo que el serializer recorre.

    No es opcional: `ProyectoSerializer` anida cinco relaciones y
    `operador_red_legal` camina las fronteras. Sin esto, un listado de 200
    proyectos son ~1 200 consultas.
    """
    return (
        py_models.Proyecto.objects
        .select_related("operador_red", "portafolio")
        .prefetch_related(
            "inversionistas__cliente",
            "info_tecnica",
            "inversores",
            "area_contactos__cliente",
            "contratos_ppa__contrato",
            "fronteras__operador_red",
        )
    )


def _filtro_ppa(request):
    """`ppa_id` (repetible) y `sin_ppa`, o `None` si no se pidió ninguno.

    Son dos preguntas distintas sobre la misma relación —"vinculado a alguno de
    estos contratos" y "sin ningún contrato"— y se combinan con **OR**, porque
    el MultiSelect de PPA del frontend permite mezclar contratos puntuales con
    el centinela "sin PPA" (ver ProyectosListView.vue). Están documentados en
    `docs/API_PROYECTOS.md`.

    Un contrato **borrado no cuenta**: borrarlo solo pone `deleted_at` y la fila
    de `ppa_contrato_proyectos` no se limpia, así que sin ese filtro un
    `ppa_id` de un contrato borrado seguiría encontrando proyectos y `sin_ppa`
    excluiría a uno cuyo único contrato ya está borrado. El resto de la API
    trata un PPA borrado como inexistente (ver `get_ppa_contratos` en
    `serializers.py`) y acá se hace igual.

    `Exists` y no un `join` + `distinct`: un proyecto con dos de los contratos
    seleccionados saldría duplicado, y el `total` de la paginación lo contaría
    dos veces.
    """
    ppa_ids = par.enteros(request, "ppa_id")
    sin_ppa = par.bandera(request, "sin_ppa")
    if not ppa_ids and not sin_ppa:
        return None

    vinculos_vivos = PpaContratoProyecto.objects.filter(
        proyecto_id=OuterRef("pk"), contrato__deleted_at__isnull=True,
    )
    condicion = Q()
    if ppa_ids:
        condicion |= Q(Exists(vinculos_vivos.filter(contrato_id__in=ppa_ids)))
    if sin_ppa:
        condicion |= ~Q(Exists(vinculos_vivos))
    return condicion


def _redividir_paneles(proyecto_id) -> dict:
    """Redivide los paneles del proyecto tras cambiar sus inversionistas.

    Las líneas de un panel son un snapshot repartido al armarlo: sin esto, el
    panel del mes en que cambió el inversionista sigue mostrando al viejo hasta
    que alguien se acuerde de redividir a mano.

    Se importa aquí adentro y no arriba para no crear un ciclo entre `proyectos`
    y `contabilidad`. Nunca levanta: ver `redividir_proyecto`.
    """
    from apps.contabilidad.services.panel import redividir_proyecto

    return redividir_proyecto(int(proyecto_id))


@class_logger_wrapper(name="Operaciones | Proyectos")
class ProyectoViewSet(viewsets.GenericViewSet):
    """Proyectos: las plantas de la plataforma.

    GET|POST /api/v1/proyectos  ·  GET|PATCH|DELETE /api/v1/proyectos/{id}
    GET  /api/v1/proyectos/pendientes
    POST /api/v1/proyectos/pendientes/{clave}/confirmar · /ignorar
    GET  /api/v1/proyectos/gen-promedio · POST /gen-promedio/recalcular
    GET  /api/v1/proyectos/lista  ·  GET /api/v1/proyectos/buscar?nombre=
    GET  /api/v1/proyectos/{id}/debug-generacion
    POST /api/v1/proyectos/{id}/vincular-sunfactory/{sunfactory_project_id}
    POST /api/v1/proyectos/{ganador_id}/merge/{perdedor_id}
    PATCH /api/v1/proyectos/{id}/servicios
    GET|PUT /api/v1/proyectos/{id}/info-tecnica
    GET|POST /api/v1/proyectos/{id}/inversores · PATCH|DELETE …/{inv_id}
    GET /api/v1/proyectos/{id}/area-contactos · PUT|DELETE …/{tipo}
    GET|POST /api/v1/proyectos/{id}/inversionistas · PATCH|DELETE …/{inv_id}

    **Un proyecto con registros operativos NO se puede borrar.** El guard mira
    tanto las relaciones del ORM como siete tablas con FK en CASCADE que no
    tienen relación declarada: sin eso, PostgreSQL borraba en silencio el
    historial de generación, los paneles contables y los registros CND.
    """

    permission_classes = [RolePermission]
    pagination_class = PaginacionConPaginas
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]
    queryset = py_models.Proyecto.objects.filter(deleted_at__isnull=True)
    lookup_value_regex = r"\d+"

    def get_serializer_class(self):
        if self.action == "create":
            return py_serializers.ProyectoCrearSerializer
        if self.action == "partial_update":
            return py_serializers.ProyectoActualizarSerializer
        return py_serializers.ProyectoSerializer

    def _proyecto(self, pk) -> py_models.Proyecto:
        proyecto = _con_relaciones().filter(pk=pk).first()
        if not proyecto:
            raise NotFound("Proyecto no encontrado")
        return proyecto

    def _simple(self, pk) -> py_models.Proyecto:
        proyecto = py_models.Proyecto.objects.filter(pk=pk).first()
        if not proyecto:
            raise NotFound("Proyecto no encontrado")
        return proyecto

    def _salida(self, pk):
        return Response(py_serializers.ProyectoSerializer(self._proyecto(pk)).data)

    # ── Listado y creación ────────────────────────────────────────────────

    def list(self, request, *args, **kwargs):
        qs = _con_relaciones().filter(deleted_at__isnull=True)
        q = request.query_params.get("q")
        if q:
            qs = qs.filter(nombre_comercial__icontains=q)
        for campo in ("estado", "tipo_proyecto"):
            if request.query_params.get(campo):
                qs = qs.filter(**{campo: request.query_params[campo]})
        if par.entero(request, "portafolio_id"):
            qs = qs.filter(portafolio_id=par.entero(request, "portafolio_id"))
        servicio = request.query_params.get("servicio")
        if servicio in SERVICIOS:
            qs = qs.filter(**{f"srv_{servicio}": True})
        filtro_ppa = _filtro_ppa(request)
        if filtro_ppa is not None:
            qs = qs.filter(filtro_ppa)

        pagina = self.paginate_queryset(qs.order_by("nombre_comercial"))
        return self.get_paginated_response(
            py_serializers.ProyectoSerializer(pagina, many=True).data
        )

    def create(self, request, *args, **kwargs):
        entrada = self.get_serializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        payload = dict(entrada.validated_data)

        verificar_unicos(payload)
        if not par.bandera(request, "forzar"):
            duplicado = buscar_duplicado_por_nombre(
                payload.get("nombre_comercial"), payload.get("tipo_proyecto"),
            )
            if duplicado:
                # `detail` estructurado y no un string plano: el frontend lo usa
                # para ofrecer "crear de todos modos" (reintenta con
                # `forzar=true`) en vez de solo mostrar un toast. A diferencia de
                # un choque de UNIQUE, esto es un aviso, no un error de datos.
                raise Conflict({
                    "mensaje": (
                        f"Ya existe un proyecto con un nombre muy parecido: "
                        f"'{duplicado.nombre_comercial}' (ID {duplicado.id})."
                    ),
                    "duplicado_nombre": True,
                    "candidato_id": duplicado.id,
                    "candidato_nombre": duplicado.nombre_comercial,
                })

        try:
            proyecto = py_models.Proyecto.objects.create(**payload)
        except IntegrityError:
            raise Conflict(
                "No se pudo guardar: algún valor único (p. ej. API ID Unergy, "
                "topic slug, ID de Solenium o de Sun Factory) ya está en uso por "
                "otro proyecto."
            )
        gestion.sincronizar_fuentes_externas(proyecto)
        return Response(
            py_serializers.ProyectoSerializer(self._proyecto(proyecto.id)).data,
            status=status.HTTP_201_CREATED,
        )

    # ── Pendientes (Sun Factory + Quoia) ──────────────────────────────────

    @action(detail=False, methods=["get"], url_path="pendientes")
    def pendientes(self, request):
        """Candidatos sin reflejar en `proyectos`, o ya existentes pero con el
        estado o la fase desincronizados. **Nunca se escriben solos.**"""
        return Response(pendientes_svc.resolver_pendientes())

    @action(
        detail=False, methods=["post"],
        url_path=r"pendientes/(?P<clave>[^/]+)/confirmar",
    )
    def confirmar_pendiente(self, request, clave=None):
        entrada = py_serializers.PendienteConfirmarSerializer(data=request.data, partial=True)
        entrada.is_valid(raise_exception=True)
        overrides = dict(entrada.validated_data)

        item = next(
            (p for p in pendientes_svc.resolver_pendientes() if p["clave"] == clave), None,
        )
        if not item:
            raise NotFound(
                "Ese candidato ya no aparece como pendiente (puede que ya se haya resuelto)."
            )

        potencia_ac_kw = overrides.get("potencia_ac_kw", item.get("potencia_ac_kw"))
        capacidad_kwp = overrides.get(
            "capacidad_instalada_kwp", item.get("capacidad_instalada_kwp")
        )

        if item["tipo_sugerencia"] == "crear":
            proyecto = self._crear_desde_pendiente(request, item, overrides)
        else:
            proyecto = self._actualizar_desde_pendiente(item)

        gestion.sincronizar_fuentes_externas(proyecto)
        if potencia_ac_kw is not None or capacidad_kwp is not None:
            self._sembrar_info_tecnica(proyecto, potencia_ac_kw, capacidad_kwp)

        return Response(
            py_serializers.ProyectoSerializer(self._proyecto(proyecto.id)).data,
            status=status.HTTP_201_CREATED,
        )

    def _crear_desde_pendiente(self, request, item, overrides) -> py_models.Proyecto:
        payload = {
            "nombre_comercial": overrides.get("nombre_comercial") or item["nombre_sugerido"],
            "tipo_proyecto": overrides.get("tipo_proyecto") or item.get("tipo_proyecto_sugerido"),
            "estado": item.get("estado_sugerido") or "en_desarrollo",
            "municipio": overrides.get("municipio") or item.get("municipio"),
            "departamento": overrides.get("departamento") or item.get("departamento"),
            "latitud": item.get("latitud"),
            "longitud": item.get("longitud"),
            "fase_construccion": item.get("fase_construccion_sugerida"),
            "origina_code": item.get("origina_code"),
            "codigo_tsf": item.get("codigo_tsf"),
            "sunfactory_project_id": item.get("sunfactory_project_id"),
            "sub_project": item.get("sub_project"),
            "project_id_solenium": item.get("project_id_solenium"),
            "origen": "pendientes",
        }
        payload = {k: v for k, v in payload.items() if v is not None}

        # El MISMO chequeo que `create`. Sin esto, dos candidatos pendientes
        # distintos —Sun Factory listó el mismo proyecto dos veces, como pasó con
        # Monterrubio— se podían confirmar por separado y crear duplicados sin
        # ningún aviso (caso real: "Astrea 1 (Calipso)", ids 274 y 275).
        if not par.bandera(request, "forzar"):
            duplicado = buscar_duplicado_por_nombre(
                payload.get("nombre_comercial"), payload.get("tipo_proyecto"),
            )
            if duplicado:
                raise Conflict({
                    "mensaje": (
                        f"Ya existe un proyecto con un nombre muy parecido: "
                        f"'{duplicado.nombre_comercial}' (ID {duplicado.id})."
                    ),
                    "duplicado_nombre": True,
                    "candidato_id": duplicado.id,
                    "candidato_nombre": duplicado.nombre_comercial,
                })
        try:
            return py_models.Proyecto.objects.create(**payload)
        except IntegrityError:
            raise Conflict(
                "No se pudo crear: algún código/ID único ya está en uso por otro proyecto."
            )

    def _actualizar_desde_pendiente(self, item) -> py_models.Proyecto:
        proyecto = py_models.Proyecto.objects.filter(pk=item["proyecto_id"]).first()
        if not proyecto:
            raise NotFound("El proyecto vinculado ya no existe")

        campos = []
        if (item.get("estado_sugerido") == "en_operacion"
                and proyecto.estado != "en_operacion"):
            proyecto.estado = "en_operacion"
            campos.append("estado")
        if item.get("fase_construccion_sugerida"):
            proyecto.fase_construccion = item["fase_construccion_sugerida"]
            campos.append("fase_construccion")
        # Backfill de vínculos y ubicación: solo lo que todavía esté vacío.
        for campo in (
            "origina_code", "codigo_tsf", "sunfactory_project_id", "sub_project",
            "project_id_solenium", "municipio", "departamento", "latitud", "longitud",
        ):
            if getattr(proyecto, campo) is None and item.get(campo) is not None:
                setattr(proyecto, campo, item[campo])
                campos.append(campo)
        if campos:
            proyecto.save(update_fields=campos)
        return proyecto

    def _sembrar_info_tecnica(self, proyecto, potencia_ac_kw, capacidad_kwp) -> None:
        """Rellena con lo que trajo el candidato, sin pisar nada.

        Cada dato va a su única casa: la potencia AC al proyecto, la capacidad
        pico (DC) a la info técnica. Antes la AC se escribía en la info técnica y
        se ESPEJABA al proyecto, porque hasta el 2026-09-10 vivía en las dos
        tablas; sin ese espejo, un proyecto creado por este camino quedaba con la
        potencia en NULL para siempre a menos que alguien volviera a editar
        Información técnica a mano (auditoría 2026-08-27: 35 proyectos con ese
        vacío, la mayoría creados justo así). Con una sola columna el espejo
        sobra y el modo de fallo tampoco puede volver.
        """
        if proyecto.potencia_ac_kw is None and potencia_ac_kw is not None:
            proyecto.potencia_ac_kw = potencia_ac_kw
            proyecto.save(update_fields=["potencia_ac_kw"])
        if capacidad_kwp is None:
            return
        it, _ = py_models.ProyectoInfoTecnica.objects.get_or_create(proyecto_id=proyecto.id)
        if it.capacidad_instalada_kwp is None:
            it.capacidad_instalada_kwp = capacidad_kwp
            it.save(update_fields=["capacidad_instalada_kwp"])

    @action(
        detail=False, methods=["post"],
        url_path=r"pendientes/(?P<clave>[^/]+)/ignorar",
    )
    def ignorar_pendiente(self, request, clave=None):
        py_models.ProyectoPendienteIgnorado.objects.get_or_create(
            clave=clave,
            defaults={"ignorado_por_usuario_id": request.user.id},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    # ── Generación mensual promedio ───────────────────────────────────────

    @action(detail=False, methods=["post"], url_path="gen-promedio/recalcular")
    def recalcular_gen_promedio(self, request):
        """Recalcula `gen_mensual_promedio_mwh` desde el histórico de generación.

        Idempotente. Por defecto **no escribe** y **no pisa** los valores
        cargados a mano: una planta sin histórico se diligencia con
        `PATCH /proyectos/{id}` y el recálculo la respeta.

        La respuesta trae `sin_datos`, `saltados` y `fallidos` con nombre y
        motivo: esa es la lista de trabajo de lo que hay que cargar a mano.
        """
        proyecto_ids = [int(v) for v in request.query_params.getlist("proyecto_id")] or None
        return Response(gen_promedio.recalcular(
            dias=par.entero(request, "dias", gen_promedio.DIAS_POR_DEFECTO, 7, 365),
            dry_run=par.bandera(request, "dry_run", defecto=True),
            force=par.bandera(request, "force"),
            proyecto_ids=proyecto_ids,
        ))

    @action(detail=False, methods=["get"], url_path="gen-promedio")
    def listar_gen_promedio(self, request):
        """El promedio de cada proyecto, con su origen y su antigüedad.

        Es la vista para saber qué falta por cargar a mano SIN tener que disparar
        un recálculo contra la API de generación.
        """
        solo_faltantes = par.bandera(request, "solo_faltantes")
        filas = []
        for p in gen_promedio.proyectos_objetivo():
            valor = (
                float(p.gen_mensual_promedio_mwh)
                if p.gen_mensual_promedio_mwh is not None else None
            )
            if solo_faltantes and valor is not None:
                continue
            filas.append({
                "id": p.id,
                "nombre_comercial": p.nombre_comercial,
                "sub_project": p.sub_project,
                "gen_mensual_promedio_mwh": valor,
                "gen_promedio_origen": p.gen_promedio_origen,
                "gen_promedio_dias": p.gen_promedio_dias,
                "gen_promedio_desde": p.gen_promedio_desde,
                "gen_promedio_hasta": p.gen_promedio_hasta,
                "gen_promedio_actualizado_en": p.gen_promedio_actualizado_en,
                # Sin identificador de monitoreo la API no lo resuelve: carga
                # manual sí o sí.
                "requiere_carga_manual": not p.sub_project,
            })
        return Response({
            "total": len(filas),
            "con_promedio": sum(
                1 for f in filas if f["gen_mensual_promedio_mwh"] is not None
            ),
            "items": filas,
        })

    # ── Consulta simple ───────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="lista")
    def lista(self, request):
        """Todos los proyectos vigentes en una llamada, con los campos justos
        para identificarlos y quedarse con el `id`.

        Sin paginar y sin filtros a propósito: es el listado de ENTRADA. Para
        filtrar o paginar está `GET /proyectos`, que además trae el objeto
        completo y es el que consume el frontend.
        """
        items = py_serializers.ProyectoListaSerializer(
            py_models.Proyecto.objects
            .filter(deleted_at__isnull=True)
            .order_by("nombre_comercial"),
            many=True,
        ).data
        return Response({"total": len(items), "items": items})

    @action(detail=False, methods=["get"], url_path="buscar")
    def buscar(self, request):
        """El detalle de un proyecto por NOMBRE. Devuelve lo mismo que
        `GET /proyectos/{id}`.

        404 si ningún nombre coincide; 409 con la lista de candidatos si coincide
        más de uno. El match es exacto sobre el nombre normalizado, no difuso.
        """
        nombre = request.query_params.get("nombre") or ""
        return self._salida(id_por_nombre(nombre))

    # ── Detalle ───────────────────────────────────────────────────────────

    def retrieve(self, request, pk=None):
        return self._salida(pk)

    def partial_update(self, request, pk=None):
        proyecto = self._simple(pk)
        entrada = self.get_serializer(proyecto, data=request.data, partial=True)
        entrada.is_valid(raise_exception=True)
        payload = dict(entrada.validated_data)
        verificar_unicos(payload, excluir_id=int(pk))

        # Editar la fecha de comercialización a mano marca el flag, para que el
        # job diario no la vuelva a pisar — salvo que se mande el flag explícito.
        if ("fecha_inicio_comercializacion" in payload
                and "fecha_comercializacion_editada_manual" not in payload):
            proyecto.fecha_comercializacion_editada_manual = True

        # Mismo criterio para el promedio de generación: escrito a mano queda
        # marcado 'manual' y el recálculo no lo pisa. Es el caso de las plantas
        # sin histórico, que es justo para lo que existe la carga manual.
        if ("gen_mensual_promedio_mwh" in payload
                and "gen_promedio_origen" not in payload):
            proyecto.gen_promedio_origen = gen_promedio.ORIGEN_MANUAL
            proyecto.gen_promedio_actualizado_en = datetime.now(timezone.utc)
            proyecto.gen_promedio_dias = None
            proyecto.gen_promedio_desde = None
            proyecto.gen_promedio_hasta = None

        for campo, valor in payload.items():
            setattr(proyecto, campo, valor)
        try:
            proyecto.save()
        except IntegrityError:
            # Backstop ante carreras o cualquier otra restricción no cubierta.
            raise Conflict(
                "No se pudo guardar: algún valor único (p. ej. API ID Unergy o "
                "topic slug) ya está en uso por otro proyecto."
            )
        if "operador_red_id" in payload:
            sincronizar_operador_red(proyecto)
        return self._salida(pk)

    def destroy(self, request, pk=None):
        proyecto = self._simple(pk)
        if gestion.motivo_bloqueo_borrado(proyecto):
            raise Conflict(
                "No se puede eliminar el proyecto porque tiene registros operativos "
                "asociados (fallas, mantenimientos, liquidaciones, contratos, etc.). "
                "Elimine primero esos registros."
            )
        gestion.borrar(proyecto)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"], url_path="debug-generacion")
    def debug_generacion(self, request, pk=None):
        """¿Las fronteras de generación de este proyecto tienen generación REAL
        hoy en Quoia?

        Usa el mismo método POR NODO que Proyectos pendientes —no por `frt_code`,
        que da 400 para algunos borders—, cacheado 1 h. Sirve para verificar si un
        proyecto marcado `en_operacion` de verdad está comercializando energía.
        """
        fronteras = list(
            Frontera.objects.filter(
                proyecto_id=pk,
                deleted_at__isnull=True,
                tipo_frontera__in=["generacion", "generacion_consumo"],
            )
        )
        if not fronteras:
            return Response({
                "tiene_frontera": False,
                "detalle": "Este proyecto no tiene frontera de generación registrada.",
            })

        from app.services.mgs.gaia_client import GaiaClient

        gaia = GaiaClient()
        if not gaia.enabled:
            raise ServicioNoDisponible("Credenciales de Quoia no configuradas.")
        try:
            borders = gaia.get_all_borders()
        except Exception as exc:
            raise ServicioNoDisponible(f"No se pudo consultar Quoia: {exc}")
        generacion_real = pendientes_svc._generacion_real_por_frt(gaia, borders)

        por_codigo = {}
        for b in borders:
            gen = b.get("frt_generation") or {}
            codigo = (gen.get("frt_code") or "").strip().lower()
            if codigo:
                por_codigo[codigo] = gen

        return Response({
            "tiene_frontera": True,
            "fronteras": [
                {
                    "codigo_frontera": f.codigo_frontera,
                    "tipo_frontera": f.tipo_frontera,
                    "last_report_date": por_codigo.get(
                        (f.codigo_frontera or "").strip().lower(), {}
                    ).get("last_report_date"),
                    "generacion_real_hoy": generacion_real.get(
                        (f.codigo_frontera or "").strip().lower(), False
                    ),
                }
                for f in fronteras
            ],
        })

    @action(
        detail=True, methods=["post"],
        url_path=r"vincular-sunfactory/(?P<sunfactory_project_id>\d+)",
    )
    def vincular_sunfactory(self, request, pk=None, sunfactory_project_id=None):
        """Confirma que un proyecto ya existente corresponde a este proyecto de
        Sun Factory.

        Resuelve las `sugerencias_vinculo` de `sync_tsf_projects`. Una vez
        confirmado, el sync lo reconoce por su id ESTABLE y no lo vuelve a
        duplicar aunque le cambien el nombre después.
        """
        proyecto = py_models.Proyecto.objects.filter(
            pk=pk, deleted_at__isnull=True,
        ).first()
        if not proyecto:
            raise NotFound("Proyecto no encontrado")
        sf_id = int(sunfactory_project_id)
        verificar_unicos({"sunfactory_project_id": sf_id}, excluir_id=int(pk))
        proyecto.sunfactory_project_id = sf_id
        proyecto.save(update_fields=["sunfactory_project_id"])
        return self._salida(pk)

    @action(detail=True, methods=["post"], url_path=r"merge/(?P<perdedor_id>\d+)")
    def merge(self, request, pk=None, perdedor_id=None):
        """Fusiona el proyecto `perdedor_id` dentro del de la URL.

        Con `dry_run=true` (por defecto) solo devuelve el reporte. Con
        `dry_run=false` ejecuta la fusión en UNA transacción y borra al perdedor.
        **Política de colisión: gana la fila del ganador.**
        """
        ganador_id, perdedor_id = int(pk), int(perdedor_id)
        if ganador_id == perdedor_id:
            raise ValidationError("El ganador y el perdedor no pueden ser el mismo proyecto.")
        ganador = py_models.Proyecto.objects.filter(pk=ganador_id).first()
        perdedor = py_models.Proyecto.objects.filter(pk=perdedor_id).first()
        if not ganador:
            raise NotFound(f"Proyecto ganador {ganador_id} no encontrado.")
        if not perdedor:
            raise NotFound(f"Proyecto perdedor {perdedor_id} no encontrado.")

        movimientos, campos_copiados = gestion.reporte_merge(ganador, perdedor)
        dry_run = par.bandera(request, "dry_run", defecto=True)
        reporte = {
            "dry_run": dry_run,
            "ganador": {"id": ganador.id, "nombre": ganador.nombre_comercial},
            "perdedor": {"id": perdedor.id, "nombre": perdedor.nombre_comercial},
            "movimientos": movimientos,
            "campos_copiados_al_ganador": campos_copiados,
            "total_filas_a_mover": sum(m["a_mover"] for m in movimientos),
            "total_filas_descartadas": sum(m["descartadas_por_colision"] for m in movimientos),
        }
        if dry_run:
            return Response(reporte)

        gestion.ejecutar_merge(
            ganador, perdedor, movimientos, campos_copiados, usuario=request.user,
        )
        reporte["ejecutado"] = True
        return Response(reporte)

    @action(detail=True, methods=["patch"], url_path="servicios")
    def servicios(self, request, pk=None):
        proyecto = self._simple(pk)
        campos = [f"srv_{s}" for s in SERVICIOS]
        tocados = [c for c in campos if c in request.data]
        for campo in tocados:
            setattr(proyecto, campo, request.data[campo])
        if tocados:
            proyecto.save(update_fields=tocados)
        return self._salida(pk)

    # ── Info técnica ──────────────────────────────────────────────────────

    @action(detail=True, methods=["get", "put"], url_path="info-tecnica")
    def info_tecnica(self, request, pk=None):
        proyecto = self._simple(pk)
        it = py_models.ProyectoInfoTecnica.objects.filter(proyecto_id=pk).first()

        if request.method == "GET":
            if not it:
                raise NotFound("Info técnica no encontrada")
            return Response(py_serializers.ProyectoInfoTecnicaSerializer(
                it, context={"proyecto": proyecto},
            ).data)

        entrada = py_serializers.ProyectoInfoTecnicaSerializer(
            it, data=request.data, partial=bool(it),
        )
        entrada.is_valid(raise_exception=True)
        # La potencia AC se edita desde este formulario pero se GUARDA en el
        # proyecto, su única casa desde el 2026-09-10. Se saca ANTES de guardar
        # la info técnica porque ya no es un campo de esa tabla.
        #
        # Un `null` no borra: igual que el espejo que esto reemplaza, solo
        # escribe cuando viene un valor. Para dejar la potencia en blanco está
        # PATCH /proyectos/{id}.
        ac = entrada.validated_data.pop("potencia_ac_kw", None)
        with transaction.atomic():
            it = entrada.save(proyecto_id=int(pk))
            if ac is not None:
                proyecto.potencia_ac_kw = ac
                proyecto.save(update_fields=["potencia_ac_kw"])
        return Response(py_serializers.ProyectoInfoTecnicaSerializer(
            it, context={"proyecto": proyecto},
        ).data)

    # ── Inversores ────────────────────────────────────────────────────────

    def _validar_suma_inversores(self, proyecto_id, nuevo_kw, excluir_id=None) -> None:
        """La suma de potencias de los inversores ACTIVOS no puede superar la
        potencia AC nominal del proyecto. Sin potencia AC configurada no se
        valida: no hay contra qué comparar.

        Un inversor retirado (`activo=False`) ya no aporta capacidad real. Antes
        contaba igual, así que reemplazar uno retirado por uno nuevo podía
        rechazarse por una suma que ya no existía físicamente.
        """
        if nuevo_kw is None:
            return
        proyecto = py_models.Proyecto.objects.filter(pk=proyecto_id).only("potencia_ac_kw").first()
        ac = (
            float(proyecto.potencia_ac_kw)
            if proyecto and proyecto.potencia_ac_kw is not None else None
        )
        if ac is None or ac <= 0:
            return
        qs = py_models.ProyectoInversor.objects.filter(proyecto_id=proyecto_id, activo=True)
        if excluir_id is not None:
            qs = qs.exclude(pk=excluir_id)
        suma = sum(float(i.potencia_nominal_kw or 0) for i in qs) + float(nuevo_kw)
        if suma > ac + 0.001:      # tolerancia por redondeos
            raise ValidationError(
                f"La suma de potencias de inversores ({suma:.1f} kW) supera la "
                f"potencia AC nominal del proyecto ({ac:.1f} kW)."
            )

    @action(detail=True, methods=["get", "post"], url_path="inversores")
    def inversores(self, request, pk=None):
        """Solo los ACTIVOS.

        Único consumidor: el selector de "qué inversor falló" al reportar una
        falla. Un inversor retirado no debería poder recibir una falla nueva
        (antes aparecía, sin ningún filtro).
        """
        self._simple(pk)
        if request.method == "GET":
            filas = (
                py_models.ProyectoInversor.objects
                .filter(proyecto_id=pk, activo=True)
                .order_by("orden", "id")
            )
            return Response(py_serializers.ProyectoInversorSerializer(filas, many=True).data)

        entrada = py_serializers.ProyectoInversorSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        self._validar_suma_inversores(pk, entrada.validated_data.get("potencia_nominal_kw"))
        try:
            entrada.save(proyecto_id=int(pk))
        except IntegrityError:
            # Doble clic, o dos peticiones casi simultáneas del prefill de
            # inversores típicos: sin esto sube como 500 crudo de Postgres.
            raise Conflict(
                f'Ya existe un inversor llamado "{entrada.validated_data.get("nombre")}" '
                f"en este proyecto."
            )
        return Response(entrada.data, status=status.HTTP_201_CREATED)

    @action(
        detail=True, methods=["patch", "delete"],
        url_path=r"inversores/(?P<inv_id>\d+)",
    )
    def inversor(self, request, pk=None, inv_id=None):
        inv = py_models.ProyectoInversor.objects.filter(pk=inv_id, proyecto_id=pk).first()
        if not inv:
            raise NotFound("Inversor no encontrado")
        if request.method == "DELETE":
            inv.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        entrada = py_serializers.ProyectoInversorSerializer(
            inv, data=request.data, partial=True,
        )
        entrada.is_valid(raise_exception=True)
        if "potencia_nominal_kw" in entrada.validated_data:
            self._validar_suma_inversores(
                pk, entrada.validated_data["potencia_nominal_kw"], excluir_id=int(inv_id),
            )
        try:
            entrada.save()
        except IntegrityError:
            raise Conflict(
                f'Ya existe un inversor llamado "{inv.nombre}" en este proyecto.'
            )
        return Response(entrada.data)

    # ── Puntero de contactos por área ─────────────────────────────────────
    # Para cada tipo (operacional/cgm/liquidación/…), el proyecto puede apuntar a
    # un Cliente concreto. Sin fila para un tipo = usa los contactos de sus
    # inversionistas vigentes.

    @action(detail=True, methods=["get"], url_path="area-contactos")
    def area_contactos(self, request, pk=None):
        self._simple(pk)
        filas = (
            ProyectoAreaContacto.objects
            .filter(proyecto_id=pk)
            .select_related("cliente")
        )
        return Response([
            {
                "id": a.id, "proyecto_id": a.proyecto_id, "tipo": a.tipo,
                "cliente_id": a.cliente_id,
                "cliente_nombre": a.cliente.razon_social_nombre,
                "created_at": a.created_at, "updated_at": a.updated_at,
            }
            for a in filas
        ])

    @action(
        detail=True, methods=["put", "delete"],
        url_path=r"area-contactos/(?P<tipo>[\w-]+)",
    )
    def area_contacto(self, request, pk=None, tipo=None):
        if request.method == "DELETE":
            area = ProyectoAreaContacto.objects.filter(proyecto_id=pk, tipo=tipo).first()
            if not area:
                raise NotFound("Este proyecto no tiene un puntero para ese tipo")
            area.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        self._simple(pk)
        entrada = py_serializers.AreaContactoSetSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        cliente_id = entrada.validated_data["cliente_id"]
        cliente = Cliente.objects.filter(pk=cliente_id).first()
        if not cliente:
            raise NotFound("Cliente no encontrado")

        area, _ = ProyectoAreaContacto.objects.update_or_create(
            proyecto_id=pk, tipo=tipo, defaults={"cliente_id": cliente_id},
        )
        return Response({
            "id": area.id, "proyecto_id": area.proyecto_id, "tipo": area.tipo,
            "cliente_id": area.cliente_id,
            "cliente_nombre": cliente.razon_social_nombre,
            "created_at": area.created_at, "updated_at": area.updated_at,
        })

    # ── Inversionistas ────────────────────────────────────────────────────

    @action(detail=True, methods=["get", "post"], url_path="inversionistas")
    def inversionistas(self, request, pk=None):
        self._simple(pk)
        if request.method == "GET":
            filas = (
                py_models.ProyectoInversionista.objects
                .filter(proyecto_id=pk)
                .select_related("cliente")
            )
            return Response(
                py_serializers.ProyectoInversionistaSerializer(filas, many=True).data
            )

        entrada = py_serializers.ProyectoInversionistaSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        if py_models.ProyectoInversionista.objects.filter(
            proyecto_id=pk, cliente_id=entrada.validated_data["cliente_id"],
        ).exists():
            raise Conflict("Este cliente ya es inversionista de este proyecto")
        inv = entrada.save(proyecto_id=int(pk))
        datos = py_serializers.ProyectoInversionistaSerializer(
            py_models.ProyectoInversionista.objects
            .select_related("cliente").get(pk=inv.id)
        ).data
        return Response(
            {**datos, "paneles_redivididos": _redividir_paneles(pk)},
            status=status.HTTP_201_CREATED,
        )

    @action(
        detail=True, methods=["patch", "delete"],
        url_path=r"inversionistas/(?P<inv_id>\d+)",
    )
    def inversionista(self, request, pk=None, inv_id=None):
        inv = py_models.ProyectoInversionista.objects.filter(
            pk=inv_id, proyecto_id=pk,
        ).first()
        if not inv:
            raise NotFound("Inversionista no encontrado")
        if request.method == "DELETE":
            inv.delete()
            _redividir_paneles(pk)
            return Response(status=status.HTTP_204_NO_CONTENT)

        entrada = py_serializers.ProyectoInversionistaSerializer(
            inv, data=request.data, partial=True,
        )
        entrada.is_valid(raise_exception=True)
        entrada.save()
        datos = py_serializers.ProyectoInversionistaSerializer(
            py_models.ProyectoInversionista.objects
            .select_related("cliente").get(pk=inv_id)
        ).data
        return Response({**datos, "paneles_redivididos": _redividir_paneles(pk)})
