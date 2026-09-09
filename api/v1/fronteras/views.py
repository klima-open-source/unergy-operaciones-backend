"""ViewSet del catálogo de fronteras y de los pendientes de Quoia."""

from datetime import date, datetime, timezone

from django.db import IntegrityError, transaction
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from api.exceptions import Conflict
from api.logging import class_logger_wrapper, log_endpoint
from api.pagination import recortar
from api.permissions import RolePermission
from apps.fronteras import models as fr_models
from apps.fronteras.services import duplicados as duplicados_service
from apps.fronteras.services import operadores_red_sync
from apps.fronteras.services import quoia as quoia_service
from apps.proyectos import models as py_models

from . import queryset as fr_queryset
from . import serializers as fr_serializers

MENSAJE_CODIGO_DUPLICADO = (
    "Ya existe una frontera con ese codigo_frontera (creada justo ahora por "
    "otra solicitud)"
)


def _forzar(request) -> bool:
    return request.query_params.get("forzar", "").lower() in ("true", "1")


@class_logger_wrapper(name="Operaciones | Fronteras")
class FronteraViewSet(viewsets.GenericViewSet):
    """Catálogo de fronteras y confirmación de los borders de Quoia.

    GET|POST /api/v1/fronteras[?proyecto_id=&tipo_frontera=&estado=
              &incluir_clientes_cgm=&skip=&limit=]
    GET|PATCH|DELETE /api/v1/fronteras/{id}     el DELETE es lógico
    GET  /api/v1/fronteras/debug-quoia-border?frt_code=
    GET  /api/v1/fronteras/quoia/pendientes
    POST /api/v1/fronteras/quoia/pendientes/{frt_code}/confirmar[?forzar=]
    POST /api/v1/fronteras/quoia/pendientes/{frt_code}/ignorar   → 204

    **`incluir_clientes_cgm` viene en `false` por defecto.** La auditoría del
    2026-08-26 encontró que el catálogo y otras cinco vistas pagaban ~216
    consultas extra en cada GET plano sin leer nunca ese campo.

    **Un código de frontera borrada RESUCITA su fila** en vez de crear una
    nueva: el índice único de la base es sobre filas vivas, así que crear otra
    dejaría la vieja invisible para siempre pese a un 201 de éxito.
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    queryset = fr_models.Frontera.objects.filter(deleted_at__isnull=True)

    def _frontera(self, pk):
        frontera = fr_queryset.con_relaciones().filter(pk=pk).first()
        if frontera is None:
            raise NotFound("Frontera no encontrada")
        return frontera

    def _salida(self, frontera):
        """Una frontera sola: sus datos derivados se consultan puntualmente."""
        corridas = fr_queryset.ultimas_generaciones([frontera.id]).get(
            frontera.id, []
        )
        return self._armar(frontera, corridas, fr_queryset.clientes_cgm(
            [frontera.proyecto_id]
        ).get(frontera.proyecto_id, []))

    @staticmethod
    def _armar(frontera, corridas, clientes):
        frontera.clientes_cgm = clientes
        if corridas:
            # «Genera de verdad» se decide contra la ventana corta, no contra
            # la última corrida sola.
            frontera.generando_actual = any(
                c.energia_final_kwh is not None and c.energia_final_kwh > 0
                for c in corridas
            )
            frontera.fecha_ultima_generacion = corridas[0].fecha
        return fr_serializers.FronteraSerializer(frontera).data

    # ── Listado ───────────────────────────────────────────────────────────

    def list(self, request, *args, **kwargs):
        consulta = fr_queryset.con_relaciones()
        for parametro in ("proyecto_id", "tipo_frontera", "estado"):
            valor = request.query_params.get(parametro)
            if valor:
                consulta = consulta.filter(**{parametro: valor})

        salto = self._entero(request, "skip", 0, 0, 10**6)
        limite = recortar(self._entero(request, "limit", 100, 1, 10**6))
        fronteras = list(
            consulta.order_by("codigo_frontera")[salto:salto + limite]
        )

        generaciones = fr_queryset.ultimas_generaciones(
            [f.id for f in fronteras]
        )
        con_cgm = request.query_params.get(
            "incluir_clientes_cgm", ""
        ).lower() in ("true", "1")
        clientes = (
            fr_queryset.clientes_cgm([f.proyecto_id for f in fronteras])
            if con_cgm else {}
        )
        return Response([
            self._armar(
                f, generaciones.get(f.id, []),
                clientes.get(f.proyecto_id, []),
            )
            for f in fronteras
        ])

    @staticmethod
    def _entero(request, nombre, defecto, minimo, maximo):
        crudo = request.query_params.get(nombre)
        if crudo in (None, ""):
            return defecto
        if not crudo.isdigit() or not minimo <= int(crudo) <= maximo:
            raise ValidationError({nombre: f"Entero entre {minimo} y {maximo}."})
        return int(crudo)

    def retrieve(self, request, *args, **kwargs):
        return Response(self._salida(self._frontera(kwargs["pk"])))

    # ── Escritura ─────────────────────────────────────────────────────────

    def _validar_fks(self, proyecto_id, operador_red_id):
        """Se validan ANTES del commit.

        Sin esto, un id inválido revienta como `IntegrityError` y el `except`
        genérico lo reporta como «ya existe ese código»: un mensaje engañoso
        que oculta la causa real.
        """
        if proyecto_id is not None and not py_models.Proyecto.objects.filter(
            pk=proyecto_id
        ).exists():
            raise NotFound("Proyecto no encontrado")
        if operador_red_id is not None and not fr_models.OperadorRed.objects.filter(
            pk=operador_red_id
        ).exists():
            raise NotFound("Operador de red no encontrado")

    def _avisar_duplicado(self, request, nombre, tipo, excluir_id=None):
        if _forzar(request):
            return
        duplicado = duplicados_service.parecida(nombre, tipo, excluir_id)
        if duplicado:
            raise Conflict(duplicados_service.aviso(duplicado))

    @staticmethod
    def _guardar(frontera, campos=None):
        """Guarda traduciendo el choque del índice único a un 409.

        `codigo_frontera` tiene un índice único case-insensitive sobre filas
        vivas: dos peticiones concurrentes con el mismo código pasan los
        chequeos previos —que corren antes del viaje a la base— y solo chocan
        al guardar.
        """
        try:
            frontera.save(update_fields=campos)
        except IntegrityError:
            raise Conflict(MENSAJE_CODIGO_DUPLICADO)

    def create(self, request, *args, **kwargs):
        entrada = fr_serializers.FronteraEscrituraSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data
        self._validar_fks(
            datos.get("proyecto").id if datos.get("proyecto") else None,
            datos.get("operador_red").id if datos.get("operador_red") else None,
        )

        codigo = datos.get("codigo_frontera")
        existente = None
        if codigo:
            # SIN filtrar `deleted_at` a propósito: si el código pertenece a
            # una frontera borrada hay que resucitarla.
            existente = fr_models.Frontera.objects.filter(
                codigo_frontera__iexact=codigo
            ).first()

        self._avisar_duplicado(
            request, datos.get("nombre_frontera"), datos.get("tipo_frontera"),
            excluir_id=existente.id if existente else None,
        )

        with transaction.atomic():
            if existente is not None:
                for campo, valor in datos.items():
                    if valor is not None:
                        setattr(existente, campo, valor)
                existente.deleted_at = None
                quoia_service.completar_medidores(existente)
                self._guardar(existente)
                frontera = existente
            else:
                frontera = fr_models.Frontera(**datos)
                quoia_service.completar_medidores(frontera)
                self._guardar(frontera)
            self._sincronizar_operador(frontera.proyecto_id)

        return Response(self._salida(self._frontera(frontera.pk)), status=201)

    def partial_update(self, request, *args, **kwargs):
        frontera = self._frontera(kwargs["pk"])
        entrada = fr_serializers.FronteraEscrituraSerializer(
            frontera, data=request.data, partial=True
        )
        entrada.is_valid(raise_exception=True)
        cambios = entrada.validated_data

        self._validar_fks(
            cambios["proyecto"].id if cambios.get("proyecto") else None,
            cambios["operador_red"].id if cambios.get("operador_red") else None,
        )

        nuevo_codigo = cambios.get("codigo_frontera")
        if nuevo_codigo and fr_models.Frontera.objects.filter(
            codigo_frontera__iexact=nuevo_codigo, deleted_at__isnull=True
        ).exclude(pk=frontera.pk).exists():
            raise Conflict(
                "Ya existe una frontera activa con ese codigo_frontera"
            )

        # No basta con mirar `nombre_frontera`: cambiar SOLO el tipo también
        # puede crear una colisión, porque la búsqueda compara dentro del mismo
        # tipo — un nombre que no chocaba en «consumo» puede chocar en
        # «generacion».
        if "nombre_frontera" in cambios or "tipo_frontera" in cambios:
            self._avisar_duplicado(
                request,
                cambios.get("nombre_frontera", frontera.nombre_frontera),
                cambios.get("tipo_frontera", frontera.tipo_frontera),
                excluir_id=frontera.pk,
            )

        with transaction.atomic():
            for campo, valor in cambios.items():
                setattr(frontera, campo, valor)
            # Un PATCH que agrega el código es la ocasión de rellenar el
            # medidor: si no, una frontera creada sin código se quedaba sin ese
            # dato para siempre.
            if nuevo_codigo:
                quoia_service.completar_medidores(frontera)
            self._guardar(frontera)
            self._sincronizar_operador(frontera.proyecto_id)

        return Response(self._salida(self._frontera(frontera.pk)))

    def destroy(self, request, *args, **kwargs):
        """Borrado LÓGICO: libera el código para que Quoia lo vuelva a ofrecer."""
        frontera = self._frontera(kwargs["pk"])
        frontera.deleted_at = datetime.now(timezone.utc)
        frontera.save(update_fields=["deleted_at"])
        return Response(status=204)

    @staticmethod
    def _sincronizar_operador(proyecto_id):
        if proyecto_id is None:
            return
        proyecto = py_models.Proyecto.objects.filter(pk=proyecto_id).first()
        if proyecto is not None:
            operadores_red_sync.sincronizar(proyecto)

    # ── Quoia ─────────────────────────────────────────────────────────────

    def _quoia(self, funcion, *args):
        try:
            return funcion(*args)
        except quoia_service.QuoiaNoConfigurado as exc:
            return Response({"detail": str(exc)}, status=503)
        except quoia_service.QuoiaNoResponde as exc:
            return Response({"detail": str(exc)}, status=503)

    @action(detail=False, methods=["get"], url_path="debug-quoia-border")
    def debug_quoia_border(self, request):
        """¿Este `frt_code` aparece en el listado de borders de Quoia?

        Diagnostica las filas sin nombre o «Sin reporte» del Excel de CGM:
        pasan exactamente cuando el código no está en ese listado.
        """
        codigo = (request.query_params.get("frt_code") or "").strip().lower()
        if not codigo:
            raise ValidationError({"frt_code": "Requerido."})

        try:
            gaia = quoia_service.cliente()
            lista = gaia.get_all_borders()
        except quoia_service.QuoiaNoConfigurado as exc:
            return Response({"detail": str(exc)}, status=503)

        encontrado = next(
            (
                {"proyecto_quoia": nombre, "tipo": categoria, **frt}
                for code, categoria, nombre, frt
                in quoia_service.iterar_frt(lista) if code == codigo
            ),
            None,
        )
        return Response({
            "frt_code": codigo,
            "total_borders_en_quoia": len(lista),
            "encontrado": encontrado is not None,
            "detalle": encontrado,
            # Con esto en True, «encontrado: false» NO significa que el código
            # no exista: significa que no se pudo preguntar.
            "fallo_consulta_quoia": gaia.ultima_llamada_fallo,
        })

    @action(detail=False, methods=["get"], url_path="quoia/pendientes")
    def quoia_pendientes(self, request):
        """Borders de Quoia sin fila en `fronteras` y sin ignorar.

        Nunca se crean solos: se listan para confirmar a mano.
        """
        from app.services.mgs.gaia_client import _mgs_number

        resultado = self._quoia(quoia_service.borders)
        if isinstance(resultado, Response):
            return resultado

        # Solo las fronteras VIVAS cuentan como registradas: una borrada libera
        # su código y su border vuelve a aparecer acá.
        #
        # Se reconoce por DOS identificadores, y el border de Quoia manda: es
        # único y no se repite, mientras que `codigo_frontera` es editable y
        # opcional en el modelo. Si a una frontera ya registrada le cambian o le
        # borran el código, con solo el código volvería a aparecer acá como
        # «nueva» y confirmarla crearía una fila duplicada.
        vivas = fr_models.Frontera.objects.filter(deleted_at__isnull=True)
        borders_registrados = set(
            vivas.filter(quoia_border_id__isnull=False)
            .values_list("quoia_border_id", flat=True)
        )
        registrados = {
            c.lower() for c in vivas
            .filter(codigo_frontera__isnull=False)
            .values_list("codigo_frontera", flat=True)
        }
        ignorados = {
            c.lower() for c in fr_models.FronteraQuoiaIgnorada.objects
            .values_list("frt_code", flat=True)
        }

        # Una sola pasada por proyecto en vez de un barrido por cada pendiente:
        # antes era O(pendientes × proyectos). El primero con cada número gana.
        por_numero: dict[int, tuple] = {}
        for pid, nombre in py_models.Proyecto.objects.filter(
            deleted_at__isnull=True
        ).values_list("id", "nombre_comercial"):
            numero = _mgs_number(nombre or "")
            if numero is not None and numero not in por_numero:
                por_numero[numero] = (pid, nombre)

        pendientes, vistos = [], set()
        for codigo, categoria, nombre_quoia, _frt in quoia_service.iterar_frt(
            resultado
        ):
            if codigo in ignorados or codigo in vistos:
                continue
            if quoia_service.ya_registrada(
                codigo, _frt.get("id"), registrados, borders_registrados
            ):
                continue
            vistos.add(codigo)
            sugerido = por_numero.get(_mgs_number(nombre_quoia))
            pendientes.append({
                "frt_code": codigo,
                "nombre_quoia": nombre_quoia,
                "categoria": categoria,
                "proyecto_sugerido_id": sugerido[0] if sugerido else None,
                "proyecto_sugerido_nombre": sugerido[1] if sugerido else None,
            })
        return Response(
            fr_serializers.PendienteSerializer(pendientes, many=True).data
        )

    @action(
        detail=False, methods=["post"],
        url_path=r"quoia/pendientes/(?P<frt_code>[^/]+)/confirmar",
    )
    @log_endpoint(name="Operaciones | Fronteras | Confirmar Quoia")
    def confirmar_quoia(self, request, frt_code=None):
        """Crea la fila real de un border, tras confirmar a qué proyecto es."""
        entrada = fr_serializers.ConfirmarQuoiaSerializer(data=request.data)
        entrada.is_valid(raise_exception=True)
        datos = entrada.validated_data
        codigo = frt_code.lower()

        if not py_models.Proyecto.objects.filter(
            pk=datos["proyecto_id"]
        ).exists():
            raise NotFound("Proyecto no encontrado")
        if fr_models.Frontera.objects.filter(
            codigo_frontera__iexact=codigo, deleted_at__isnull=True
        ).exists():
            raise Conflict("Ya existe una frontera con ese codigo_frontera")

        # Confirmar gana sobre un «ignorar» anterior: si alguien decide
        # registrarlo ahora, no tiene sentido que siga excluido.
        fr_models.FronteraQuoiaIgnorada.objects.filter(frt_code=codigo).delete()

        resultado = self._quoia(quoia_service.borders)
        if isinstance(resultado, Response):
            return resultado

        encontrado = next(
            (
                (categoria, nombre, frt)
                for code, categoria, nombre, frt
                in quoia_service.iterar_frt(resultado) if code == codigo
            ),
            None,
        )
        if encontrado is None:
            raise NotFound("Ese frt_code ya no aparece en Quoia")
        categoria, nombre_quoia, frt = encontrado

        nombre_base = datos.get("nombre_frontera") or nombre_quoia or codigo
        nombre = (
            f"{nombre_base} Consumo"
            if categoria == "consumo" and not datos.get("nombre_frontera")
            else nombre_base
        )
        # «consumo_auxiliar» y «consumo_propio» son subtipos que alguien tiene
        # que elegir: sin nada explícito, el default de un border de consumo es
        # el tipo genérico, no un subtipo que nadie pidió.
        tipo = datos.get("tipo_frontera") or (
            "generacion" if categoria == "generacion" else "consumo"
        )

        # Dos capas, y hacen cosas distintas:
        #
        #   · por id (el `frt_code` de arriba y el `quoia_border_id` de acá):
        #     duro, no se puede forzar. El border de Quoia es único y no se
        #     repite, así que resuelve la identidad sin ambigüedad.
        #   · por nombre parecido (`_avisar_duplicado`, más abajo): blando, se
        #     puede forzar. Cubre lo que los ids NO ven -- una frontera creada
        #     a mano no recibe `quoia_border_id` (solo se escribe acá), y si
        #     además quedó sin `codigo_frontera` (`null=True` en el modelo) no
        #     tiene ningún id con qué reconocerse. Confirmar su border crearía
        #     una segunda fila para el mismo punto físico.
        border_id = frt.get("id")
        if border_id is not None and fr_models.Frontera.objects.filter(
            quoia_border_id=border_id, deleted_at__isnull=True
        ).exists():
            raise Conflict(
                "Ese border de Quoia ya está registrado en otra frontera"
            )

        self._avisar_duplicado(request, nombre, tipo)

        with transaction.atomic():
            # Si el código es de una frontera BORRADA se resucita esa fila:
            # conserva su id y su historial.
            frontera = fr_models.Frontera.objects.filter(
                codigo_frontera__iexact=codigo
            ).first()
            if frontera is None:
                frontera = fr_models.Frontera(codigo_frontera=codigo)
            else:
                frontera.deleted_at = None

            frontera.proyecto_id = datos["proyecto_id"]
            frontera.nombre_frontera = nombre
            frontera.tipo_frontera = tipo
            frontera.estado = "activa"
            frontera.quoia_border_id = border_id
            frontera.fecha_registro_asic = self._fecha(frt.get("init_date"))
            quoia_service.fijar_medidores(
                frontera, *quoia_service.info_de_medidores(codigo)
            )
            # Repetido a propósito: si un «ignorar» concurrente se coló entre
            # el primer borrado y este commit, la fila ignorada quedaría viva
            # junto a la frontera recién creada.
            fr_models.FronteraQuoiaIgnorada.objects.filter(
                frt_code=codigo
            ).delete()
            self._guardar(frontera)
            self._sincronizar_operador(frontera.proyecto_id)

        return Response(self._salida(self._frontera(frontera.pk)), status=201)

    @staticmethod
    def _fecha(valor):
        if not valor:
            return None
        try:
            return date.fromisoformat(valor)
        except ValueError:
            return None

    @action(
        detail=False, methods=["post"],
        url_path=r"quoia/pendientes/(?P<frt_code>[^/]+)/ignorar",
    )
    @log_endpoint(name="Operaciones | Fronteras | Ignorar Quoia")
    def ignorar_quoia(self, request, frt_code=None):
        """Marca un border como «no aplica» (medidor de prueba, de un tercero…)."""
        codigo = frt_code.lower()
        if fr_models.Frontera.objects.filter(
            codigo_frontera__iexact=codigo, deleted_at__isnull=True
        ).exists():
            raise Conflict(
                "Ya existe una frontera activa con ese codigo_frontera -- no "
                "se puede ignorar"
            )
        # Dos clics rápidos sobre el mismo código: el segundo pierde la carrera
        # contra el chequeo, pero para quien lo pidió ya quedó ignorado.
        fr_models.FronteraQuoiaIgnorada.objects.get_or_create(
            frt_code=codigo,
            defaults={"ignorado_por_usuario_id": request.user.id},
        )
        return Response(status=204)
