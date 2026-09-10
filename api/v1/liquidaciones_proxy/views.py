"""ViewSet del proxy a la API de Liquidaciones de Unergy.

El frontend no habla directo con api.unergy.io: las credenciales de la cuenta de
servicio viven solo en el servidor. Estos endpoints cruzan los proyectos de esta
base con su configuración de liquidaciones.
"""

from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from api.logging import class_logger_wrapper, get_logger, log_endpoint
from api.pagination import recortar
from api.permissions import RolePermission
from apps.liquidaciones.services import agregados
from apps.liquidaciones.services import api_externa as api
from apps.liquidaciones.services import proxy as proxy_service
from apps.proyectos import models as py_models

from . import serializers as liq_serializers

logger = get_logger("Liquidaciones | proxy")

# Códigos según de quién es el fallo: si la API externa no responde es 502; si
# ni siquiera hay credenciales configuradas, 503 (el servidor no está listo).
HTTP_API_EXTERNA = 502
HTTP_NO_DISPONIBLE = 503


def _entero(request, nombre, minimo, maximo, defecto=None):
    crudo = request.query_params.get(nombre)
    if crudo in (None, ""):
        return defecto
    if not crudo.isdigit() or not minimo <= int(crudo) <= maximo:
        raise ValidationError({nombre: f"Entero entre {minimo} y {maximo}."})
    return int(crudo)


@class_logger_wrapper(name="Operaciones | Liquidaciones | API externa")
class LiquidacionesApiViewSet(viewsets.GenericViewSet):
    """Proxy a la API de Liquidaciones y el ciclo mensual.

    Proyectos:  GET|PATCH /proyectos[/{id}]  ·  GET|PATCH /subproyectos
    AC Power:   GET /ac-power
    Tareas:     GET /tareas/{task_id}
    Facturas:   GET|POST /facturas-xm
    Ciclo:      POST /ciclo/{ipp,ftp,liquidar,repartir,estado-resultados,
                cruce-facturas,diagnostico}
    Consulta:   GET /despachos · /consumo · /ipp · /costos · /catalogos
    Contratos:  GET|POST /contratos-energia  ·  POST /costos/excel

    **El orden del ciclo importa**: liquidar → repartir → estado de resultados →
    cruce. IPP, FTP y facturas son independientes entre sí.

    Las tareas del ciclo devuelven un `task_id` y **el sondeo lo hace el
    frontend**, no el servidor: tardan minutos y dejar una petición HTTP colgada
    todo ese rato no sirve para una pantalla.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ["get", "post", "patch", "head", "options"]
    queryset = py_models.Proyecto.objects.none()

    def _llamar(self, funcion, *args, estado=HTTP_API_EXTERNA, **kwargs):
        """Traduce `LiquidacionesAPIError` al código que corresponda."""
        try:
            return funcion(*args, **kwargs)
        except api.LiquidacionesAPIError as exc:
            return Response({"detail": str(exc)}, status=estado)

    # ── Proyectos ─────────────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="proyectos")
    def proyectos(self, request):
        """Proyectos de esta base con su configuración de liquidaciones."""
        try:
            config = {
                p["nombre_topico"]: p
                for p in api.listar_proyectos() if p.get("nombre_topico")
            }
        except api.LiquidacionesAPIError as exc:
            return Response({"detail": str(exc)}, status=HTTP_NO_DISPONIBLE)

        return Response([
            proxy_service.fila_de_proyecto(
                proyecto, config.get(proxy_service.topico(proyecto) or "", {})
            )
            for proyecto in proxy_service.proyectos_vivos()
        ])

    @action(
        detail=False, methods=["get", "patch"],
        url_path=r"proyectos/(?P<proyecto_id>[0-9]+)",
    )
    @log_endpoint(name="Operaciones | Liquidaciones | Proyecto")
    def proyecto(self, request, proyecto_id=None):
        proyecto = py_models.Proyecto.objects.filter(
            pk=proyecto_id, deleted_at__isnull=True
        ).first()
        if proyecto is None:
            raise NotFound("Proyecto no encontrado")
        topico = proxy_service.topico(proyecto)

        if request.method == "GET":
            datos = {}
            if topico:
                try:
                    datos = api.obtener_proyecto(topico)
                except api.LiquidacionesAPIError:
                    # El proyecto puede no existir en la API todavía: se
                    # devuelve sin configuración en vez de fallar.
                    datos = {}
            return Response(proxy_service.fila_de_proyecto(proyecto, datos))

        if not topico:
            raise ValidationError(
                "El proyecto no tiene código base (API ID Unergy) y no se "
                "puede identificar en la API de Liquidaciones."
            )
        entrada = liq_serializers.ProyectoUpdateSerializer(
            data=request.data, partial=True
        )
        entrada.is_valid(raise_exception=True)
        if not entrada.validated_data:
            raise ValidationError("No se enviaron campos para actualizar")

        resultado = self._llamar(
            api.actualizar_proyecto, topico, entrada.validated_data
        )
        if isinstance(resultado, Response):
            return resultado
        return Response(proxy_service.fila_de_proyecto(proyecto, resultado))

    # ── Subproyectos ──────────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="subproyectos")
    def subproyectos(self, request):
        return self._respuesta(self._llamar(
            api.listar_subproyectos,
            project=request.query_params.get("project"),
            topic=request.query_params.get("topic"),
        ))

    @action(
        detail=False, methods=["patch"],
        # El tópico va con `.+` porque varios traen espacios («MGS Mapale»).
        url_path=r"subproyectos/(?P<topico>.+)",
    )
    @log_endpoint(name="Operaciones | Liquidaciones | Subproyecto")
    def subproyecto(self, request, topico=None):
        entrada = liq_serializers.SubproyectoUpdateSerializer(
            data=request.data, partial=True
        )
        entrada.is_valid(raise_exception=True)
        if not entrada.validated_data:
            raise ValidationError("No se enviaron ids para actualizar")
        return self._respuesta(self._llamar(
            api.actualizar_subproyecto, topico, entrada.validated_data
        ))

    # ── AC Power y tareas ─────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="ac-power")
    def ac_power(self, request):
        """AC Power total por grupo, tal como lo ve la API de Liquidaciones.

        **No se calcula sumando las filas de pantalla**: esas salen de cruzar
        por tópico con esta base, y un proyecto sin cruce quedaría fuera del
        divisor de la prorrata sin que se note.
        """
        resultado = self._llamar(api.totales_ac_power)
        if isinstance(resultado, Response):
            return resultado
        conocidos = set(proxy_service.nombres_por_topico())
        return Response({
            "generador": resultado["generador"],
            "comercializador": resultado["comercializador"],
            "topicos_sin_cruce": sorted(
                t for t in resultado["topicos"] if t not in conocidos
            ),
        })

    @action(
        detail=False, methods=["get"],
        url_path=r"tareas/(?P<task_id>[^/]+)",
    )
    def tarea(self, request, task_id=None):
        """Estado de una tarea del ciclo, ya normalizado.

        Ojo: un `task_id` inexistente responde «en curso» para siempre, porque
        la API no distingue «en cola» de «no existe» — quien sondee necesita su
        propio límite de tiempo.
        """
        return self._respuesta(self._llamar(api.consultar_tarea, task_id))

    # ── Facturas de XM ────────────────────────────────────────────────────

    @action(detail=False, methods=["get", "post"], url_path="facturas-xm")
    @log_endpoint(name="Operaciones | Liquidaciones | Facturas XM")
    def facturas_xm(self, request):
        if request.method == "GET":
            resultado = self._llamar(
                api.listar_facturas_xm,
                month=_entero(request, "month", 1, 12),
                year=_entero(request, "year", 2020, 2100),
                version=request.query_params.get("version"),
                processing_status=request.query_params.get("processing_status"),
                agente=request.query_params.get("agente"),
            )
            if isinstance(resultado, Response):
                return resultado
            return Response(agregados.build_facturas_xm(resultado))

        archivos = request.FILES.getlist("files")
        if not archivos:
            raise ValidationError({"files": "Falta al menos un archivo."})
        if len(archivos) > api.MAX_FACTURAS_POR_LOTE:
            raise ValidationError(
                f"Máximo {api.MAX_FACTURAS_POR_LOTE} facturas por lote."
            )

        preparados = []
        for archivo in archivos:
            contenido = archivo.read()
            if len(contenido) > api.MAX_BYTES_POR_FACTURA:
                raise ValidationError(
                    f"«{archivo.name}» pesa más de "
                    f"{api.MAX_BYTES_POR_FACTURA // (1024 * 1024)} MB."
                )
            if not (archivo.name or "").lower().endswith(".pdf"):
                raise ValidationError(f"«{archivo.name}» no es un PDF.")
            preparados.append((
                archivo.name, contenido,
                archivo.content_type or "application/pdf",
            ))

        # La clave de Gemini puede venir del formulario cuando la del servidor
        # no sirve o no está puesta. No se guarda, no se registra y no vuelve en
        # la respuesta: solo se reenvía a la API que lee los PDF.
        resultado = self._llamar(
            api.subir_facturas_xm, preparados,
            request.data.get("version", "txf"),
            api_key=request.data.get("api_key"),
        )
        if isinstance(resultado, Response):
            return resultado
        return Response(resultado, status=201)

    # ── Ciclo mensual ─────────────────────────────────────────────────────

    def _periodo(self, request, serializer=None):
        entrada = (serializer or liq_serializers.PeriodoSerializer)(
            data=request.data
        )
        entrada.is_valid(raise_exception=True)
        return entrada.validated_data

    @action(detail=False, methods=["post"], url_path="ciclo/ipp")
    def ciclo_ipp(self, request):
        """IPP del mes, del DANE. Síncrono: devuelve el valor, no una tarea."""
        datos = self._periodo(request)
        resultado = self._llamar(api.obtener_ipp, datos["month"], datos["year"])
        if isinstance(resultado, Response):
            return resultado
        return Response({
            "month": datos["month"], "year": datos["year"], "ipp": resultado,
        })

    @action(detail=False, methods=["post"], url_path="ciclo/ftp")
    @log_endpoint(name="Operaciones | Liquidaciones | FTP")
    def ciclo_ftp(self, request):
        """Descarga los ocho archivos del FTP de XM. Exige los SIC/FRT cargados."""
        return self._tarea(request, api.descargar_archivos_xm)

    @action(detail=False, methods=["post"], url_path="ciclo/liquidar")
    @log_endpoint(name="Operaciones | Liquidaciones | Liquidar")
    def ciclo_liquidar(self, request):
        """Liquida los contratos del período. Exige el FTP ya descargado."""
        return self._tarea(request, api.liquidar_contratos)

    @action(detail=False, methods=["post"], url_path="ciclo/repartir")
    @log_endpoint(name="Operaciones | Liquidaciones | Repartir")
    def ciclo_repartir(self, request):
        """Reparte las facturas de XM entre proyectos, a prorrata del AC Power."""
        datos = self._periodo(request, liq_serializers.RepartoSerializer)
        return self._respuesta(self._llamar(
            api.repartir_facturas_xm,
            datos["month"], datos["year"], datos["total_ac_power"],
            datos["override"], datos["version"], datos.get("last_version"),
        ), envolver_tarea=True)

    @action(detail=False, methods=["post"], url_path="ciclo/estado-resultados")
    @log_endpoint(name="Operaciones | Liquidaciones | Estado de resultados")
    def ciclo_estado_resultados(self, request):
        """Genera el .xlsx del estado de resultados en la carpeta de Drive."""
        return self._tarea(request, api.generar_estado_resultados)

    @action(detail=False, methods=["post"], url_path="ciclo/cruce-facturas")
    @log_endpoint(name="Operaciones | Liquidaciones | Cruce de facturas")
    def ciclo_cruce_facturas(self, request):
        """Verifica que lo repartido cuadre con la factura de XM."""
        return self._tarea(request, api.generar_cruce_facturas)

    @action(detail=False, methods=["post"], url_path="ciclo/diagnostico")
    def ciclo_diagnostico(self, request):
        """Por qué un proyecto no sale en el estado de resultados."""
        datos = self._periodo(request, liq_serializers.DiagnosticoSerializer)
        return self._respuesta(self._llamar(
            api.diagnosticar_proyecto,
            datos["project"], datos["month"], datos["year"], datos["version"],
        ))

    def _tarea(self, request, funcion):
        datos = self._periodo(request)
        return self._respuesta(
            self._llamar(
                funcion, datos["month"], datos["year"], datos["version"]
            ),
            envolver_tarea=True,
        )

    @staticmethod
    def _respuesta(resultado, envolver_tarea: bool = False):
        if isinstance(resultado, Response):
            return resultado
        if envolver_tarea:
            return Response({"task_id": resultado})
        return Response(resultado)

    # ── Consulta ──────────────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="despachos")
    def despachos(self, request):
        """Despachos ya liquidados, día por día y por contrato.

        Sale del histórico de liquidaciones de mercado, que es el dato CRUDO que
        produce «Liquidar». Antes se armaba aplanando el estado de resultados,
        que viene consolidado por mes: así se recuperan la fecha, el precio y el
        código del contrato, que ahí no estaban.
        """
        resultado = self._llamar(
            api.listar_liquidaciones_mercado,
            year=_entero(request, "year", 2020, 2100),
            month=_entero(request, "month", 1, 12),
            version=request.query_params.get("version", "txf"),
            data_type=request.query_params.get("data_type"),
            project=request.query_params.get("project"),
        )
        if isinstance(resultado, Response):
            return resultado
        return Response(agregados.build_despachos(
            resultado, proxy_service.nombres_por_topico()
        ))

    @action(detail=False, methods=["get"], url_path="consumo")
    def consumo(self, request):
        resultado = self._llamar(
            api.listar_contratos_despachados,
            year=_entero(request, "year", 2020, 2100),
            month=_entero(request, "month", 1, 12),
            version=request.query_params.get("version", "txf"),
            project=request.query_params.get("project"),
            date=request.query_params.get("fecha"),
        )
        if isinstance(resultado, Response):
            return resultado
        return Response(agregados.build_consumo(
            resultado, proxy_service.nombres_por_topico()
        ))

    @action(detail=False, methods=["get"], url_path="ipp")
    def ipp(self, request):
        """IPP del DANE ya consultados, del más reciente al más antiguo."""
        resultado = self._llamar(
            api.listar_ipp_historico,
            year=_entero(request, "year", 2020, 2100),
            month=_entero(request, "month", 1, 12),
        )
        if isinstance(resultado, Response):
            return resultado
        return Response(agregados.build_ipp_historico(resultado))

    @action(detail=False, methods=["post"], url_path="ipp/sincronizar")
    @log_endpoint(name="Operaciones | Liquidaciones | Sincronizar IPP")
    def ipp_sincronizar(self, request):
        """Trae el IPP del DANE de la API y lo guarda en nuestro `ipp_mensual`.

        Es el mismo número que ya usa Facturación, pero se llenaba a mano y se
        quedaba corto: el 2026-09-10 la API tenía 15 meses que nosotros no,
        incluido el que se iba a facturar.

        A mano y no por horario, a propósito: el IPP sale una vez al mes y quien
        liquida decide cuándo traerlo.
        """
        from apps.ppa.services import ipp as ipp_svc

        try:
            return Response(ipp_svc.sincronizar())
        except api.LiquidacionesAPIError as exc:
            return Response({"detail": str(exc)}, status=HTTP_API_EXTERNA)

    @action(detail=False, methods=["get"], url_path="costos")
    def costos(self, request):
        try:
            filas = api.listar_costos(
                project=request.query_params.get("project"),
                payment_type=request.query_params.get("payment_type"),
                version=request.query_params.get("version"),
            )
            tipos = {
                t["name"]: t for t in api.listar_catalogos()["tipos_costo"]
            }
        except api.LiquidacionesAPIError as exc:
            return Response({"detail": str(exc)}, status=HTTP_API_EXTERNA)

        solo_con_valor = request.query_params.get(
            "solo_con_valor", "true"
        ).lower() not in ("false", "0")
        return Response(agregados.build_costos(
            filas, tipos, proxy_service.nombres_por_topico(),
            grupo=request.query_params.get("grupo"),
            mes=_entero(request, "mes", 1, 12),
            anio=_entero(request, "anio", 2020, 2100),
            solo_con_valor=solo_con_valor,
            page=_entero(request, "page", 1, 10**6, 1),
            size=recortar(_entero(request, "size", 1, 10**6, 100)),
        ))

    @action(detail=False, methods=["get"], url_path="catalogos")
    def catalogos(self, request):
        """Empresas, precios de energía y tipos de costo. Son datos fijos."""
        return self._respuesta(self._llamar(api.listar_catalogos))

    # ── Contratos de energía ──────────────────────────────────────────────

    @action(detail=False, methods=["get", "post"], url_path="contratos-energia")
    @log_endpoint(name="Operaciones | Liquidaciones | Contratos de energía")
    def contratos_energia(self, request):
        if request.method == "GET":
            try:
                datos = agregados.build_contratos_energia(
                    api.listar_contratos(),
                    api.listar_contrato_proyectos(),
                    api.listar_cantidades(),
                    api.listar_catalogos(),
                    proxy_service.nombres_por_topico(),
                )
            except api.LiquidacionesAPIError as exc:
                return Response({"detail": str(exc)}, status=HTTP_API_EXTERNA)
            return Response(datos)

        return self._crear_contrato(request)

    def _crear_contrato(self, request):
        """Crea el contrato, lo vincula a sus proyectos y carga pisos y techos.

        Va en ese orden porque cada paso necesita el id del anterior. **La API
        externa no ofrece transacción**: si un vínculo falla se informa qué
        alcanzó a crearse, en vez de dejar un contrato huérfano en silencio.
        """
        entrada = liq_serializers.ContratoEnergiaSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data
        proyectos = datos.pop("proyectos", [])

        try:
            contrato = api.crear_contrato(
                {k: v for k, v in datos.items() if v is not None}
            )
        except api.LiquidacionesAPIError as exc:
            return Response({"detail": str(exc)}, status=HTTP_API_EXTERNA)

        # Sin id no se puede vincular nada, y el contrato puede haber quedado
        # creado igual: se dice, en vez de reventar con un KeyError.
        contrato_id = contrato.get("id")
        if contrato_id is None:
            return Response({"detail": (
                "La API de Liquidaciones no devolvió el id del contrato, así que "
                "no se pudo vincular ningún proyecto. Puede haber quedado creado "
                "sin proyectos: revísalo antes de volver a intentarlo."
            )}, status=HTTP_API_EXTERNA)

        creados = []
        for proyecto in proyectos:
            # Se atrapa CUALQUIER error, no solo los de la API. Un fallo
            # inesperado aquí escapaba de DRF y salía como un 500 en HTML, sin
            # decir que el contrato ya estaba creado — y quien reintentaba
            # terminaba con un duplicado en producción.
            try:
                vinculo = api.vincular_contrato_proyecto({
                    "contract_energy": contrato_id,
                    "project": proyecto["project"],
                    **(
                        {"energy_price": proyecto["energy_price"]}
                        if proyecto.get("energy_price") is not None else {}
                    ),
                })
                vinculo_id = vinculo.get("id")
                if vinculo_id is None:
                    raise api.LiquidacionesAPIError(
                        "la API no devolvió el id del vínculo"
                    )
                for concepto, horas in (
                    ("floor", proyecto.get("floor")),
                    ("roof", proyecto.get("roof")),
                ):
                    if horas:
                        api.crear_cantidades({
                            "contract_energy_project": vinculo_id,
                            "concept_type": concepto,
                            "hours": horas,
                        })
            except Exception as exc:
                if not isinstance(exc, api.LiquidacionesAPIError):
                    logger.exception(
                        "Fallo inesperado vinculando el contrato %s con «%s»",
                        contrato_id, proyecto["project"],
                    )
                return Response({"detail": (
                    f"El contrato {contrato_id} SÍ se creó, pero falló al "
                    f'vincular «{proyecto["project"]}»: {exc}. Alcanzaron a '
                    f"vincularse {len(creados)}. No lo vuelvas a crear: "
                    f"completa los vínculos que falten."
                )}, status=HTTP_API_EXTERNA)
            creados.append({
                "id": vinculo_id,
                "proyecto": proyecto["project"],
                "precio_energia_id": proyecto.get("energy_price"),
                "tiene_piso": bool(proyecto.get("floor")),
                "tiene_techo": bool(proyecto.get("roof")),
            })

        return Response({
            "id": contrato_id,
            "fecha_desde": contrato.get("date_from"),
            "fecha_hasta": contrato.get("date_to"),
            "codigo": contrato.get("code"),
            "tipo_contrato": contrato.get("contract_type"),
            "tipo_tarifa": contrato.get("tariff_price_type"),
            "porcentaje": contrato.get("percentage"),
            "empresa_id": contrato.get("company"),
            "empresa": None,
            "proyectos": creados,
        }, status=201)

    @action(
        detail=False, methods=["patch"],
        url_path=r"contratos-energia/(?P<contrato_id>[0-9]+)",
    )
    @log_endpoint(name="Operaciones | Liquidaciones | Editar contrato de energía")
    def contrato_energia(self, request, contrato_id=None):
        """Edita un contrato, sus vínculos con proyectos y sus pisos y techos.

        Es un PATCH de verdad: lo que no venga en el cuerpo no se toca. Cada
        entrada de `proyectos` con `id` es un vínculo que ya existe y se
        corrige; sin `id` es uno nuevo y se crea. Igual con `piso_id`/`techo_id`.

        **Desvincular un proyecto no se puede**: la API externa no expone
        `DELETE` en ninguno de los tres recursos.

        Como tampoco hay transacción, si un paso falla se responde diciendo qué
        alcanzó a cambiarse, en vez de dejar a quien edita sin saber en qué
        estado quedó el contrato.
        """
        entrada = liq_serializers.ContratoEnergiaUpdateSerializer(
            data=request.data, partial=True
        )
        entrada.is_valid(raise_exception=True)
        datos = dict(entrada.validated_data)
        proyectos = datos.pop("proyectos", None)

        hechos = []
        try:
            if datos:
                api.actualizar_contrato(contrato_id, datos)
                hechos.append("los datos del contrato")

            for proyecto in proyectos or []:
                vinculo_id = proyecto.get("id")
                if vinculo_id is None:
                    creado = api.vincular_contrato_proyecto({
                        "contract_energy": int(contrato_id),
                        "project": proyecto["project"],
                        **({"energy_price": proyecto["energy_price"]}
                           if proyecto.get("energy_price") is not None else {}),
                    })
                    vinculo_id = creado.get("id")
                    if vinculo_id is None:
                        raise api.LiquidacionesAPIError(
                            "la API no devolvió el id del vínculo creado"
                        )
                    hechos.append(f'se vinculó «{proyecto["project"]}»')
                else:
                    cambios = {
                        k: proyecto[k] for k in ("project", "energy_price")
                        if k in proyecto
                    }
                    if cambios:
                        api.actualizar_contrato_proyecto(vinculo_id, cambios)
                        hechos.append(f"se actualizó el vínculo {vinculo_id}")

                for concepto, clave_id, clave_horas in (
                    ("floor", "piso_id", "floor"),
                    ("roof", "techo_id", "roof"),
                ):
                    horas = proyecto.get(clave_horas)
                    if not horas:
                        continue
                    curva_id = proyecto.get(clave_id)
                    if curva_id is None:
                        api.crear_cantidades({
                            "contract_energy_project": vinculo_id,
                            "concept_type": concepto, "hours": horas,
                        })
                        hechos.append(f"se creó el {concepto}")
                    else:
                        api.actualizar_cantidades(curva_id, {"hours": horas})
                        hechos.append(f"se actualizó el {concepto}")
        except Exception as exc:
            if not isinstance(exc, api.LiquidacionesAPIError):
                logger.exception("Fallo inesperado editando el contrato %s", contrato_id)
            return Response({"detail": (
                f"No se pudo terminar de editar el contrato {contrato_id}: {exc}. "
                f"Sí alcanzó a aplicarse: {', '.join(hechos) or 'nada'}."
            )}, status=HTTP_API_EXTERNA)

        return Response({"id": int(contrato_id), "aplicado": hechos})

    @action(detail=False, methods=["post"], url_path="costos/excel")
    @log_endpoint(name="Operaciones | Liquidaciones | Excel de costos")
    def costos_excel(self, request):
        """Carga masiva de costos e ingresos fijos desde un Excel."""
        archivo = request.FILES.get("file")
        if archivo is None:
            raise ValidationError({"file": "Falta el archivo."})
        if not (archivo.name or "").lower().endswith((".xlsx", ".xls")):
            raise ValidationError(f"«{archivo.name}» no es un Excel.")
        return self._respuesta(self._llamar(
            api.subir_excel_costos,
            archivo.name, archivo.read(),
            archivo.content_type
            or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ))
