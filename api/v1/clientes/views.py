"""ViewSet de Clientes — 25 rutas.

El detalle del cliente cuelga todo de `/{id}`: contactos, documentos, tasas de
servicio, sus plantas, sus contratos y el panel 360. Un solo ViewSet porque
ninguno de esos recursos existe fuera de un cliente.

Los agregados (vista comercial, servicios-contratos, panel) los arma
`apps/clientes/services/vistas.py`; acá no se calcula nada.
"""

import uuid
from collections import defaultdict
from pathlib import Path

from django.db import IntegrityError, transaction
from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from api.exceptions import Conflict, ServicioNoDisponible
from api.logging import class_logger_wrapper
from api.pagination import PaginacionConPaginas
from api.permissions import RolePermission
from apps.clientes import models as cl_models
from apps.clientes.services import gestion, vistas
from apps.clientes.services.panel import proyectos_por_cliente
from apps.contratos.models import ContratoServicio
from apps.fronteras.models import Frontera
from apps.ppa.models import PpaContrato, PpaContratoProyecto
from apps.proyectos.models import Proyecto, ProyectoInversionista

from . import serializers as cl_serializers

UPLOADS_DIR = Path("uploads/clientes")
ALLOWED_MIME = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB


def _ruta_local(archivo_url: str | None) -> Path | None:
    """La ruta en disco de un `archivo_url` servido desde /static/uploads/."""
    if not archivo_url or not archivo_url.startswith("/static/uploads/"):
        return None
    return Path(archivo_url.lstrip("/").replace("static/", "", 1))


@class_logger_wrapper(name="Operaciones | Clientes")
class ClienteViewSet(viewsets.GenericViewSet):
    """Clientes: la razón social con la que Unergy contrata.

    GET|POST /api/v1/clientes            ·  GET /api/v1/clientes/vista-comercial
    GET|PATCH|DELETE /api/v1/clientes/{id}
    POST /api/v1/clientes/{id}/test-correo
    GET /api/v1/clientes/{id}/tasas-servicio  ·  PUT …/tasa-servicio
    DELETE /api/v1/clientes/{id}/tasa-servicio/{tasa_id}
    GET|POST /api/v1/clientes/{id}/contactos
    PATCH|DELETE /api/v1/clientes/{id}/contactos/{c_id}
    GET|POST /api/v1/clientes/{id}/documentos
    PATCH|DELETE /api/v1/clientes/{id}/documentos/{doc_id}
    POST /api/v1/clientes/{id}/documentos/{doc_id}/archivo
    GET /api/v1/clientes/{id}/proyectos · /fronteras · /contratos-ppa
    GET /api/v1/clientes/{id}/servicios-contratos · /panel
    POST /api/v1/clientes/{ganador_id}/merge/{perdedor_id}

    **Un cliente nunca se borra físico**: `deleted_at`. Y no se puede borrar si es
    inversionista de un proyecto o tiene oportunidades comerciales.
    """

    permission_classes = [RolePermission]
    pagination_class = PaginacionConPaginas
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]
    queryset = cl_models.Cliente.objects.filter(deleted_at__isnull=True)
    lookup_value_regex = r"\d+"

    def get_serializer_class(self):
        if self.action in ("create", "partial_update"):
            return cl_serializers.ClienteEntradaSerializer
        if self.action == "list":
            return cl_serializers.ClienteListSerializer
        return cl_serializers.ClienteSerializer

    def _cliente(self, pk) -> cl_models.Cliente:
        cliente = (
            cl_models.Cliente.objects
            .prefetch_related("documentos_comerciales")
            .filter(pk=pk)
            .first()
        )
        if not cliente:
            raise NotFound("Cliente no encontrado")
        return cliente

    # ── CRUD ──────────────────────────────────────────────────────────────

    def list(self, request, *args, **kwargs):
        qs = cl_models.Cliente.objects.filter(deleted_at__isnull=True)
        q = request.query_params.get("q")
        if q:
            qs = qs.filter(razon_social_nombre__icontains=q)
        pagina = self.paginate_queryset(qs.order_by("razon_social_nombre"))
        return self.get_paginated_response(
            cl_serializers.ClienteListSerializer(pagina, many=True).data
        )

    def _avisar_nombre_parecido(self, request, nombre, excluir_id=None):
        """409 estructurado si ya hay un cliente con nombre muy parecido.

        `forzar=true` lo salta. El aviso NO bloquea —hay razones reales para dos
        nombres parecidos— pero tiene que aparecer: el algoritmo es el mismo de
        proyectos y fronteras, y detecta lo que un match exacto no ve
        ("Quantum" contra "Quantum Energy Ingenieria S.A.S.").
        """
        if request.query_params.get("forzar", "").strip().lower() in ("1", "true", "yes", "on"):
            return
        duplicado = gestion.buscar_duplicado(nombre, excluir_id=excluir_id)
        if duplicado:
            raise Conflict({
                "mensaje": (
                    f"Ya existe un cliente con un nombre muy parecido: "
                    f"'{duplicado.razon_social_nombre}' (ID {duplicado.id})."
                ),
                "duplicado_nombre": True,
                "candidato_id": duplicado.id,
                "candidato_nombre": duplicado.razon_social_nombre,
            })

    def create(self, request, *args, **kwargs):
        """`forzar=true` crea igual aunque exista un cliente con nombre parecido."""
        entrada = self.get_serializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = dict(entrada.validated_data)
        contactos = datos.pop("contactos", [])

        self._avisar_nombre_parecido(request, datos.get("razon_social_nombre"))

        # Dos filas con el mismo (email, tipo) son la misma persona repetida en
        # el formulario, no un error que valga tumbar el alta: `contactos` tiene
        # UNIQUE (cliente_id, email, tipo). El email ya viene normalizado por el
        # serializer, así que "Juan@X.com" y "juan@x.com" cuentan como uno.
        unicos: dict[tuple, dict] = {}
        for c in contactos:
            unicos.setdefault((c["email"], c["tipo"]), c)
        contactos = list(unicos.values())

        # Todo en UNA transacción. Antes el cliente se creaba con su propio
        # commit (autocommit) y los contactos iban después: si ese INSERT
        # fallaba, salía un 500 y el cliente quedaba creado igual -- el usuario
        # veía un error, reintentaba, y entonces chocaba con el NIT de la fila
        # que su intento anterior sí había dejado. En FastAPI esto no pasaba
        # porque era un `flush` + un solo `commit` (ver app/api/v1/clientes.py).
        #
        # Atrapar `IntegrityError` acá adentro es seguro porque no se ejecuta
        # ninguna consulta entre el `except` y el `raise`: la excepción sale del
        # bloque y lo revierte entero.
        with transaction.atomic():
            try:
                cliente = cl_models.Cliente.objects.create(**datos)
            except IntegrityError:
                raise Conflict("Ya existe un cliente con ese NIT/cédula.")
            if contactos:
                try:
                    cl_models.Contacto.objects.bulk_create([
                        cl_models.Contacto(cliente_id=cliente.id, **c)
                        for c in contactos
                    ])
                except IntegrityError:
                    raise Conflict(
                        "No se pudo guardar la lista de contactos: hay dos con el "
                        "mismo correo y el mismo tipo."
                    )
        return Response(
            cl_serializers.ClienteSerializer(self._cliente(cliente.id)).data,
            status=status.HTTP_201_CREATED,
        )

    def retrieve(self, request, pk=None):
        return Response(cl_serializers.ClienteSerializer(self._cliente(pk)).data)

    def partial_update(self, request, pk=None):
        cliente = self._cliente(pk)
        entrada = self.get_serializer(cliente, data=request.data, partial=True)
        entrada.is_valid(raise_exception=True)
        datos = dict(entrada.validated_data)
        datos.pop("contactos", None)   # solo se leen al crear

        # El nombre se revisa también al editar. Antes solo se revisaba al
        # crear, así que renombrar un cliente y dejarlo igual a otro pasaba en
        # silencio -- la protección se saltaba con dos clics. `buscar_duplicado`
        # ya aceptaba `excluir_id` justo para esto y nadie se lo pasaba: sin
        # excluirse, todo cliente sería su propio duplicado.
        if datos.get("razon_social_nombre"):
            self._avisar_nombre_parecido(
                request, datos["razon_social_nombre"], excluir_id=cliente.id,
            )

        for campo, valor in datos.items():
            setattr(cliente, campo, valor)
        cliente.save()
        return Response(cl_serializers.ClienteSerializer(self._cliente(pk)).data)

    def destroy(self, request, pk=None):
        """Soft-delete. Un cliente ya borrado se ve como "no encontrado", igual
        que en el resto de la API."""
        cliente = cl_models.Cliente.objects.filter(pk=pk, deleted_at__isnull=True).first()
        if not cliente:
            raise NotFound("Cliente no encontrado")
        motivo = gestion.motivo_bloqueo_borrado(cliente.id)
        if motivo:
            raise Conflict(f"No se puede eliminar: este cliente {motivo}. Desvincúlalo primero.")
        gestion.borrar(cliente)
        return Response(status=status.HTTP_204_NO_CONTENT)

    # ── Vistas agregadas ──────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="vista-comercial")
    def vista_comercial(self, request):
        return Response(vistas.vista_comercial())

    @action(detail=True, methods=["get"], url_path="panel")
    def panel(self, request, pk=None):
        cliente = self._cliente(pk)
        salida = vistas.panel_360(cliente)
        salida["cliente"] = cl_serializers.ClienteSerializer(cliente).data
        # `cliente` va primero en la respuesta original.
        return Response({"cliente": salida.pop("cliente"), **salida})

    @action(detail=True, methods=["get"], url_path="servicios-contratos")
    def servicios_contratos(self, request, pk=None):
        self._cliente(pk)
        return Response(vistas.servicios_contratos(int(pk)))

    # ── Correo de prueba ──────────────────────────────────────────────────

    @action(detail=True, methods=["post"], url_path="test-correo")
    def test_correo(self, request, pk=None):
        """Verifica que los correos operacionales del cliente están bien
        configurados. Body: `{"email": "destino@empresa.com"}`."""
        from app.services.email_service import send_test_email

        cliente = self._cliente(pk)
        email = (request.data.get("email") or "").strip()
        if not email:
            raise ValidationError("Debes indicar el campo 'email'")
        try:
            send_test_email(to_email=email, cliente_nombre=cliente.razon_social_nombre)
        except RuntimeError as exc:
            raise ServicioNoDisponible(str(exc))
        return Response({"ok": True, "enviado_a": email})

    # ── Tasas por servicio ────────────────────────────────────────────────

    @action(detail=True, methods=["get"], url_path="tasas-servicio")
    def tasas_servicio(self, request, pk=None):
        self._cliente(pk)
        filas = (
            cl_models.ClienteTasaServicio.objects
            .filter(cliente_id=pk)
            .order_by("servicio", "proyecto_id")
        )
        return Response(cl_serializers.TasaServicioSerializer(filas, many=True).data)

    @action(detail=True, methods=["put"], url_path="tasa-servicio")
    def tasa_servicio(self, request, pk=None):
        """Crea o actualiza la excepción de tasa de `(cliente, servicio[, proyecto])`.
        Cada `_pct` en null hereda la tasa general del cliente."""
        self._cliente(pk)
        entrada = cl_serializers.TasaServicioSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = dict(entrada.validated_data)
        fila, _creada = cl_models.ClienteTasaServicio.objects.update_or_create(
            cliente_id=pk,
            servicio=datos["servicio"],
            proyecto_id=datos.get("proyecto_id"),
            defaults={
                campo: datos.get(campo)
                for campo in ("iva_pct", "retencion_pct", "reteiva_pct", "reteica_pct")
            },
        )
        return Response(cl_serializers.TasaServicioSerializer(fila).data)

    @action(
        detail=True, methods=["delete"],
        url_path=r"tasa-servicio/(?P<tasa_id>\d+)",
    )
    def eliminar_tasa_servicio(self, request, pk=None, tasa_id=None):
        cl_models.ClienteTasaServicio.objects.filter(pk=tasa_id, cliente_id=pk).delete()
        return Response({"ok": True})

    # ── Contactos ─────────────────────────────────────────────────────────
    # Correos reales de esta razón social, por área. Aplican por defecto a todos
    # sus proyectos, salvo que un proyecto apunte a otro Cliente para ese `tipo`.

    @action(detail=True, methods=["get", "post"], url_path="contactos")
    def contactos(self, request, pk=None):
        self._cliente(pk)
        if request.method == "GET":
            filas = cl_models.Contacto.objects.filter(cliente_id=pk)
            return Response(cl_serializers.ContactoSerializer(filas, many=True).data)
        entrada = cl_serializers.ContactoSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        entrada.save(cliente_id=pk)
        return Response(entrada.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch", "delete"], url_path=r"contactos/(?P<c_id>\d+)")
    def contacto(self, request, pk=None, c_id=None):
        contacto = cl_models.Contacto.objects.filter(pk=c_id, cliente_id=pk).first()
        if not contacto:
            raise NotFound("Contacto no encontrado")
        if request.method == "DELETE":
            contacto.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        entrada = cl_serializers.ContactoSerializer(contacto, data=request.data, partial=True)
        entrada.is_valid(raise_exception=True)
        entrada.save()
        return Response(entrada.data)

    # ── Documentos comerciales (ofertas / contratos) ──────────────────────

    @action(detail=True, methods=["get", "post"], url_path="documentos")
    def documentos(self, request, pk=None):
        self._cliente(pk)
        if request.method == "GET":
            filas = (
                cl_models.ClienteDocumentoComercial.objects
                .filter(cliente_id=pk)
                .order_by("tipo", "fecha")
            )
            return Response(cl_serializers.ClienteDocumentoSerializer(filas, many=True).data)
        entrada = cl_serializers.ClienteDocumentoSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        entrada.save(cliente_id=pk)
        return Response(entrada.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["patch", "delete"], url_path=r"documentos/(?P<doc_id>\d+)")
    def documento(self, request, pk=None, doc_id=None):
        doc = cl_models.ClienteDocumentoComercial.objects.filter(
            pk=doc_id, cliente_id=pk
        ).first()
        if not doc:
            raise NotFound("Documento no encontrado")
        if request.method == "DELETE":
            ruta = _ruta_local(doc.archivo_url)
            if ruta and ruta.exists():
                ruta.unlink(missing_ok=True)
            doc.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        entrada = cl_serializers.ClienteDocumentoSerializer(doc, data=request.data, partial=True)
        entrada.is_valid(raise_exception=True)
        entrada.save()
        return Response(entrada.data)

    @action(
        detail=True, methods=["post"],
        url_path=r"documentos/(?P<doc_id>\d+)/archivo",
    )
    def documento_archivo(self, request, pk=None, doc_id=None):
        doc = cl_models.ClienteDocumentoComercial.objects.filter(
            pk=doc_id, cliente_id=pk
        ).first()
        if not doc:
            raise NotFound("Documento no encontrado")

        archivo = request.FILES.get("archivo")
        if archivo is None:
            raise ValidationError("Falta el archivo")
        if archivo.content_type not in ALLOWED_MIME:
            raise ValidationError("Tipo de archivo no permitido. Use PDF, JPG o PNG.")
        contenido = archivo.read()
        if len(contenido) > MAX_FILE_SIZE:
            raise ValidationError("El archivo supera el límite de 20 MB")

        anterior = _ruta_local(doc.archivo_url)
        if anterior:
            anterior.unlink(missing_ok=True)

        ext = Path(archivo.name).suffix.lower() if archivo.name else ".pdf"
        nombre_guardado = f"{uuid.uuid4().hex}{ext}"
        carpeta = UPLOADS_DIR / str(pk)
        carpeta.mkdir(parents=True, exist_ok=True)
        (carpeta / nombre_guardado).write_bytes(contenido)

        doc.archivo_url = f"/static/uploads/clientes/{pk}/{nombre_guardado}"
        doc.archivo_nombre = archivo.name or nombre_guardado
        doc.save(update_fields=["archivo_url", "archivo_nombre"])
        return Response(cl_serializers.ClienteDocumentoSerializer(doc).data)

    # ── Vínculos: proyectos, fronteras, PPAs ──────────────────────────────

    def _roles_por_proyecto(self, cliente_id, ids):
        """Por qué cada planta está en la lista de este cliente.

        Una planta puede entrar por más de un motivo (inversionista Y
        contratante del contrato de esa misma planta), y sin el rol la pestaña
        crece sin que se entienda de dónde salieron las filas nuevas: alguien
        abre un cliente y ve plantas en las que no tiene participación.
        """
        roles = defaultdict(list)

        def anotar(proyecto_ids, etiqueta):
            for pid in proyecto_ids:
                if etiqueta not in roles[pid]:
                    roles[pid].append(etiqueta)

        anotar(
            ProyectoInversionista.objects
            .filter(cliente_id=cliente_id, proyecto_id__in=ids)
            .values_list("proyecto_id", flat=True),
            "inversionista",
        )
        for campo, etiqueta in (("contratante_id", "contratante"),
                                ("prestador_id", "prestador")):
            anotar(
                ContratoServicio.objects
                .filter(proyecto_id__in=ids, **{campo: cliente_id})
                .values_list("proyecto_id", flat=True),
                etiqueta,
            )
        anotar(
            PpaContratoProyecto.objects
            .filter(proyecto_id__in=ids, contrato__deleted_at__isnull=True)
            .filter(Q(contrato__comprador_id=cliente_id)
                    | Q(contrato__vendedor_id=cliente_id))
            .values_list("proyecto_id", flat=True),
            "ppa",
        )
        return roles

    def _plantas_del_cliente(self, cliente_id) -> set[int]:
        """Las plantas que TOCAN a este cliente, con el mismo criterio que el
        Resumen: inversionista, contratante/prestador de un contrato de
        servicio, o cubierta por uno de sus PPA.

        Antes estas dos pestañas usaban solo `ProyectoInversionista`, así que
        una planta que llegaba por contrato o por PPA salía en el Resumen y NO
        acá -- la inconsistencia que se reportó el 2026-09-10. El criterio
        amplio es el que está bien pensado: `contratante_id`/`prestador_id` casi
        nunca se pueblan (el campo del wizard es texto libre), y sin el camino
        por planta el panel quedaba vacío aunque el cliente tuviera contratos
        reales -- el caso Quantum, inversionista de GD Sirius y GD Elektra,
        cuyos contratos de representación no lo nombran como contratante.
        """
        return proyectos_por_cliente({int(cliente_id)}).get(int(cliente_id), set())

    @action(detail=True, methods=["get"], url_path="proyectos")
    def proyectos(self, request, pk=None):
        """Las plantas de este cliente, con el rol por el que entra cada una."""
        self._cliente(pk)
        ids = self._plantas_del_cliente(pk)
        if not ids:
            return Response([])
        roles = self._roles_por_proyecto(int(pk), ids)
        return Response([
            {
                "id": p.id,
                "nombre_comercial": p.nombre_comercial,
                "estado": p.estado,
                # `potencia_ac_kw` y no `potencia_kwp`: la columna se llama
                # `potencia_ac_kw` pero lo que guarda es potencia AC
                # (revisado con Sara el 2026-09-10). Renombrar la columna son
                # 171 usos entre los dos repos y una migración; el nombre del
                # campo de ESTA respuesta se corrige sin costo. El front leía
                # `potencia_ac_kw`, que nunca llegó: la potencia no se
                # mostraba en ninguna planta.
                "potencia_ac_kw": float(p.potencia_ac_kw)
                if p.potencia_ac_kw else None,
                "departamento": p.departamento,
                "municipio": p.municipio,
                "roles": roles.get(p.id, []),
            }
            for p in Proyecto.objects.filter(id__in=ids, deleted_at__isnull=True)
            .order_by("nombre_comercial")
        ])

    @action(detail=True, methods=["get"], url_path="fronteras")
    def fronteras(self, request, pk=None):
        """Las fronteras de las plantas de este cliente -- mismo criterio que la
        pestaña Proyectos, o una planta que llega por contrato no mostraba
        ninguna frontera."""
        self._cliente(pk)
        ids = self._plantas_del_cliente(pk)
        if not ids:
            return Response([])
        filas = (
            Frontera.objects
            .filter(
                proyecto_id__in=ids,
                deleted_at__isnull=True,
                proyecto__deleted_at__isnull=True,
            )
            .order_by("codigo_frontera")
        )
        return Response([
            {
                "id": f.id,
                "codigo_frontera": f.codigo_frontera,
                "nombre_frontera": f.nombre_frontera,
                "tipo_frontera": f.tipo_frontera,
                "estado": f.estado,
                "proyecto_id": f.proyecto_id,
            }
            for f in filas
        ])

    @action(detail=True, methods=["get"], url_path="contratos-ppa")
    def contratos_ppa(self, request, pk=None):
        """Los PPA donde este cliente es comprador o vendedor."""
        from django.db.models import F

        self._cliente(pk)
        contratos = (
            PpaContrato.objects
            .filter(deleted_at__isnull=True)
            .filter(Q(comprador_id=pk) | Q(vendedor_id=pk))
            .order_by(F("fecha_inicio").desc(nulls_last=True))
        )
        return Response([
            {
                "id": c.id,
                "numero_codigo_contrato": c.numero_codigo_contrato,
                "nombre_interno": c.nombre_interno,
                "comprador_nombre": c.comprador_nombre,
                "vendedor_nombre": c.vendedor_nombre,
                "fecha_inicio": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
                "fecha_fin": c.fecha_fin.isoformat() if c.fecha_fin else None,
                "tipo_contrato": c.tipo_contrato,
                "rol": "comprador" if c.comprador_id == int(pk) else "vendedor",
            }
            for c in contratos
        ])

    # ── Fusión de duplicados ──────────────────────────────────────────────

    @action(detail=True, methods=["post"], url_path=r"merge/(?P<perdedor_id>\d+)")
    def merge(self, request, pk=None, perdedor_id=None):
        """Fusiona el cliente `perdedor_id` dentro del de la URL.

        Con `dry_run=true` (por defecto) solo devuelve el reporte de lo que
        pasaría. Con `dry_run=false` ejecuta la fusión en UNA transacción y da de
        baja (soft-delete) al perdedor. **Política de colisión: gana la fila del
        ganador.**
        """
        ganador_id, perdedor_id = int(pk), int(perdedor_id)
        if ganador_id == perdedor_id:
            raise ValidationError("El ganador y el perdedor no pueden ser el mismo cliente.")

        vivos = cl_models.Cliente.objects.filter(deleted_at__isnull=True)
        ganador = vivos.filter(pk=ganador_id).first()
        perdedor = vivos.filter(pk=perdedor_id).first()
        if not ganador:
            raise NotFound(f"Cliente ganador {ganador_id} no encontrado.")
        if not perdedor:
            raise NotFound(f"Cliente perdedor {perdedor_id} no encontrado.")

        movimientos, campos_copiados = gestion.reporte_merge(ganador, perdedor)
        crudo = request.query_params.get("dry_run")
        dry_run = True if crudo is None else crudo.strip().lower() in ("1", "true", "yes", "on")

        reporte = {
            "dry_run": dry_run,
            "ganador": {"id": ganador.id, "nombre": ganador.razon_social_nombre},
            "perdedor": {"id": perdedor.id, "nombre": perdedor.razon_social_nombre},
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
