"""La lectura del correo de mandatos: correrla a mano, diagnosticarla y su bitácora.

Lo que queda de `/api/v1/mandatos`. La lectura llena `finanzas_mandatos`, que se ve
en Liquidaciones › Mandatos (`api/v1/finanzas_mandatos`). La vista vieja de
Costos › Mandatos --la carga del ZIP, la tabla `mandatos` y la lista
`mandato_inversionistas`-- se retiró el 2026-10-13: se usó una sola vez (una carga
de prueba del 24-jun) y la reemplazó Liquidaciones › Mandatos. Con ella se fue
`POST /correos/{id}/revertir`, que deshacía cambios en la tabla vieja y no en
`finanzas_mandatos`: para la lectura de hoy no revertía nada.
"""

import threading
from datetime import datetime, timezone

from django.db import close_old_connections
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from api.exceptions import Conflict, NoProcesable
from api.logging import class_logger_wrapper, log_endpoint
from api.permissions import RolePermission
from apps.mandatos import models as md_models

LIMITE_BITACORA = 500
DIAS_MAXIMOS_INGESTA = 1825


@class_logger_wrapper(name="Operaciones | Mandatos")
class MandatoViewSet(viewsets.GenericViewSet):
    """La lectura del correo de mandatos.

    POST   /api/v1/mandatos/ejecutar-ingesta  solo admin; ESCRIBE
    GET    /api/v1/mandatos/diagnostico-imap  solo lee
    GET    /api/v1/mandatos/correos           la bitácora
    """

    permission_classes = [RolePermission]
    pagination_class = None
    http_method_names = ["get", "post", "head", "options"]
    queryset = md_models.MandatoCorreo.objects.all()

    def get_permissions(self):
        # Ejecutar la ingesta no es una consulta: es una acción que escribe.
        if self.action == "ejecutar_ingesta":
            self.required_role = ["admin"]
        return super().get_permissions()

    # ── Ingesta por correo ────────────────────────────────────────────────

    @action(detail=False, methods=["post"], url_path="ejecutar-ingesta")
    @log_endpoint(name="Operaciones | Mandatos | Ejecutar ingesta")
    def ejecutar_ingesta(self, request):
        """Corre la lectura del buzón AHORA, sin esperar al cron de las :05.

        **Esto escribe**: hace lo mismo que el cron. Es POST y pide admin a
        propósito — un GET se dispara con abrir la URL y un prefetch del
        navegador podría ejecutarlo sin que nadie lo pidiera.

        Volver a correrlo es inofensivo: la deduplicación va por `Message-ID`.

        `reprocesar_desde` **destruye información que no se reconstruye**: borra
        las filas de bitácora desde esa fecha, y con ellas el `estado_previo`
        del que sale el botón de revertir. Pide fecha explícita en vez de un
        `todo=true` para que sea imposible vaciar la tabla por descuido.
        """
        from apps.mandatos.services.correo_sync import (
            ingesta_en_curso, revisar_correos_async,
        )

        if ingesta_en_curso():
            raise Conflict(
                "Ya hay una corrida en curso. Espera a que termine antes de "
                "lanzar otra."
            )

        dias = request.query_params.get("dias", "30")
        if not dias.isdigit() or not 1 <= int(dias) <= DIAS_MAXIMOS_INGESTA:
            raise ValidationError(
                {"dias": f"Entero entre 1 y {DIAS_MAXIMOS_INGESTA}."}
            )

        borradas = 0
        reprocesar = request.query_params.get("reprocesar_desde")
        if reprocesar:
            try:
                desde = datetime.strptime(
                    reprocesar.strip()[:10], "%Y-%m-%d"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                raise NoProcesable(
                    "reprocesar_desde debe ser YYYY-MM-DD"
                )
            borradas, _ = md_models.MandatoCorreo.objects.filter(
                fecha__gte=desde
            ).delete()

        # En segundo plano a propósito: leer 90 días son MINUTOS —cada PDF se
        # abre para revisar firmas— y el proxy corta la conexión mucho antes,
        # devolviendo un 502 que parece un fallo del backend cuando el proceso
        # sigue corriendo.
        def corriendo():
            try:
                revisar_correos_async(int(dias))
            finally:
                close_old_connections()

        threading.Thread(target=corriendo, daemon=True).start()
        return Response({
            "ok": True, "estado": "arrancó en segundo plano", "dias": int(dias),
            "bitacora_borrada": borradas if reprocesar else 0,
            "reprocesado_desde": reprocesar,
            "como_ver_avance": (
                "GET /api/v1/mandatos/correos?limite=500 -- el conteo va "
                "subiendo mientras procesa"
            ),
        })

    @action(detail=False, methods=["get"], url_path="diagnostico-imap")
    def diagnostico_imap(self, request):
        """Prueba la conexión IMAP a demanda. Solo lee: no toca base ni buzón."""
        from apps.mandatos.services.diagnostico import diagnostico_imap

        return Response(diagnostico_imap())

    @action(detail=False, methods=["get"], url_path="correos")
    def correos(self, request):
        """Bitácora de correos leídos, del más reciente al más viejo."""
        limite = request.query_params.get("limite", "100")
        if not limite.isdigit():
            raise ValidationError({"limite": "Debe ser entero."})
        consulta = md_models.MandatoCorreo.objects.order_by("-fecha")
        if request.query_params.get("solo_revision", "").lower() in ("true", "1"):
            consulta = consulta.filter(requiere_revision=True)

        return Response([
            {
                "id": f.id, "fecha": f.fecha.isoformat(),
                "remitente": f.remitente, "asunto": f.asunto,
                "fuente": f.fuente, "clasificacion": f.clasificacion,
                "resultado": f.resultado,
                "requiere_revision": f.requiere_revision,
                "revertido": f.revertido, "detalle": f.detalle,
            }
            for f in consulta[:min(int(limite), LIMITE_BITACORA)]
        ])
