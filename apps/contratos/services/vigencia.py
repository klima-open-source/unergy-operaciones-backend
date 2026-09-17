"""Vigencia de un contrato: una sola definición, calculada, nunca guardada.

Responde "¿este contrato está vivo hoy?" combinando las dos mitades que el
sistema tenía separadas y que ninguna veía a la otra:

- **La decisión humana** — `contratos_servicio.estado`, que alguien pone en el
  wizard. `terminado` es un cierre deliberado y manda sobre cualquier fecha.
- **El paso del tiempo** — `fecha_fin`. Nadie la vuelve a mirar, y ahí estaba el
  agujero: medido el 2026-09-17, **8 contratos dicen `estado='vigente'` con la
  fecha fin ya pasada**, algunos desde 2025. Los filtros que solo miraban
  `estado` los daban por vivos.

**No se guarda.** Un campo almacenado habría que recalcularlo cuando pasa el
tiempo, y un campo que nadie recalcula es exactamente el problema que esto
resuelve — el mismo de las banderas `srv_*` de `Proyecto`.

Reemplaza tres definiciones que convivían:

- `ESTADOS_CONTRATO_VIVO` (`api/v1/monitoreo/queryset.py`) — informe FMO de O&M
- `ESTADOS_QUE_AVISAN` (`apps/contratos/services/alertas_representacion.py`) —
  alerta de aniversario de indexación
- `semaforo_contrato()` (`apps/clientes/services/panel.py`) — el color de la
  vista Servicios, que miraba la fecha pero ignoraba `estado`

Las dos primeras eran la MISMA tupla con dos nombres. La tercera veía la otra
mitad. Ver `docs/SERVICIOS_AGRUPACION.md`.
"""

from __future__ import annotations

from datetime import date

from django.db.models import Q

from apps.clientes.services.panel import UMBRAL_POR_VENCER_DIAS, semaforo_contrato

# ── Los valores que puede tomar la vigencia calculada ─────────────────────
VIGENTE = "vigente"
POR_VENCER = "por_vencer"
VENCIDO = "vencido"
TERMINADO = "terminado"

#: Los `estado` que NO cierran el contrato por decisión humana.
#:
#: `en_renovacion` cuenta como vivo: es justo cuando la tarifa nueva importa
#: —la razón que ya daba `ESTADOS_QUE_AVISAN`—. Nunca se ha usado: 0 filas hoy
#: y 0 menciones en los 147.102 registros de `audit_log` (mayo a septiembre de
#: 2026, cuando dejó de escribirse). Se conserva porque el wizard lo ofrece y
#: tres vistas lo pintan.
ESTADOS_NO_CERRADOS = ("vigente", "en_renovacion")

#: Las vigencias que cuentan como "el contrato está vivo hoy".
#: `por_vencer` sí cuenta: vence pronto, pero todavía rige.
VIGENCIAS_VIVAS = (VIGENTE, POR_VENCER)


def de_contrato(contrato, hoy: date, umbral_dias: int = UMBRAL_POR_VENCER_DIAS) -> str:
    """La vigencia real de un `ContratoServicio`.

    La decisión humana gana: un contrato `terminado` sale `terminado` aunque su
    fecha fin no haya llegado. Para el resto decide la fecha, con la misma regla
    y el mismo umbral que ya usaba el semáforo de la vista Servicios -- un
    contrato sin `fecha_fin` es indefinido, y por lo tanto vigente (138 de 160
    están así).
    """
    if contrato.estado not in ESTADOS_NO_CERRADOS:
        return TERMINADO
    return semaforo_contrato(contrato.fecha_fin, hoy, umbral_dias)


def esta_vivo(contrato, hoy: date) -> bool:
    """Si el contrato rige hoy. Lo que una bandera `srv_*` debería responder."""
    return de_contrato(contrato, hoy) in VIGENCIAS_VIVAS


def filtro_vivos(hoy: date) -> Q:
    """La misma definición, como condición de ORM.

    Existe porque `de_contrato()` no sirve dentro de un `filter()`: sin esto,
    cada módulo escribe su propia versión del criterio, que es justo como
    aparecieron las tres definiciones que esto unifica.

    `fecha_fin__isnull=True` es la mitad que faltaba en los filtros viejos: un
    contrato indefinido no tiene fecha de vencimiento y sigue rigiendo.
    """
    return (
        Q(estado__in=ESTADOS_NO_CERRADOS)
        & (Q(fecha_fin__isnull=True) | Q(fecha_fin__gte=hoy))
    )


def filtro_ppa_vivos(hoy: date) -> Q:
    """Lo mismo para `PpaContrato`, que NO tiene columna `estado`.

    Su vigencia es puramente de fechas -- por eso el front la calcula aparte en
    `ppaVigencia.ts`. Acá se usa la misma mitad de fecha que `filtro_vivos`, sin
    la del estado, en vez de dar por vivo cualquier PPA con solo existir.
    """
    return Q(fecha_fin__isnull=True) | Q(fecha_fin__gte=hoy)
