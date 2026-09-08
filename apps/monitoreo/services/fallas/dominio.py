"""Las reglas de una falla: SLA, clasificación estructurada y adjuntos.

Puerto de los helpers de `app/api/v1/fallas.py` — 1 457 líneas donde toda la
lógica vivía dentro de los endpoints.

Tres reglas que estaban repetidas o implícitas y acá tienen UN solo dueño:

- **`sincronizar_resolucion` es el único punto que sella `fecha_resolucion` y
  `sla_cumplido`.** Antes la regla estaba copiada en `update_falla` y en
  `add_seguimiento`: corregir una copia y olvidar la otra las dejaba
  desincronizadas. `sla_cumplido` es SIEMPRE calculado, nunca manual.

- **`aplicar_clasificacion` asigna `tipo_id` SIEMPRE, incluso `None`.** Dejar el
  valor previo era la causa de títulos como "Fusible de string quemado" en fallas
  de red. Si el tipo estructurado no existe en el catálogo, `tipo_id` queda en
  `None` y el título se arma al vuelo desde `clasificacion` (ver `titulo.py`).

- **Una falla pendiente de reclasificar no se puede cerrar.** Decisión de negocio
  del 2026-09-02: mejor bloquear que dejar que se cierre y la causa real nunca se
  confirme — pasó con 840 de 851 casos históricos.
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

from rest_framework.exceptions import ValidationError

from api.exceptions import Conflict, NoProcesable
from apps.monitoreo.models import Falla, FallaCatTipo, FallaIntervalo, FallaInversor
from apps.plataforma.services.fechas import hoy_col
from apps.monitoreo.services.fallas.estructura import (
    es_subtipo_pendiente, etiqueta_subtipo, get_categoria, tipo_codigo,
    validar_clasificacion,
)

_COL_TZ = timezone(timedelta(hours=-5))

# Límite de SLA por nivel de prioridad, en horas.
DEFAULT_SLA_HOURS = {
    1: 8,     # critica
    2: 24,    # grave
    3: 72,    # media
    4: 168,   # leve (7 días)
}

# Huella del integrador externo que reporta desbalances de tensión y
# reconectadores en cero bajo el subtipo genérico "sin identificar". No vive en
# este repo y no se puede tocar su lógica (auditoría 2026-09-02). Se usa tanto en
# el filtro `?pendiente_reclasificar=true` como en el bloqueo de cierre.
# Límite conocido: es indistinguible de una persona reportando ese mismo subtipo
# a mano — hoy no hay ningún caso así en producción.
BOT_DESCONEXION_CATEGORIA = "red"
BOT_DESCONEXION_SUBTIPO = "desconexion_sin_identificar"

# Precio medio de la energía y factor de planta solar, para la estimación de
# impacto económico.
PRECIO_ENERGIA_COP_KWH = 800.0
SOLAR_CAPACITY_FACTOR = 0.18


def sla_limite_horas_efectivo(falla: Falla) -> int:
    """El límite que realmente aplica: el personalizado o el de su prioridad.

    `sla_limite_horas` está poblado en el 0.06 % de las fallas (auditoría
    2026-09-02), así que sin este default la UI mostraba "Sin límite" casi
    siempre aunque el cálculo de SLA sí funcionaba por debajo.
    """
    nivel = falla.prioridad.nivel if falla.prioridad_id else None
    return falla.sla_limite_horas or DEFAULT_SLA_HOURS.get(nivel, 72)


def inicio_sla(falla: Falla) -> datetime:
    """El instante en que ARRANCA el reloj del SLA, en hora Colombia.

    Suma `hora_identificacion`, que se captura y se guarda pero que el cálculo
    ignoraba: el límite se anclaba a las 00:00 del día. Efecto real —una crítica
    (SLA 8 h) identificada a las 9:00 a.m. vencía a las 8:00 a.m. del mismo día,
    o sea **nacía vencida**; una grave (24 h) detectada a las 18:00 tenía seis
    horas reales en vez de veinticuatro. Y como de acá salen el `sla_cumplido`
    que se sella al cerrar, el "en riesgo"/"vencido" del tablero y el badge
    "Cumplido/Incumplido" del detalle, los cuatro estaban mal a la vez.

    Es el mismo patrón que `tiempo_afectacion_horas` ya usaba 80 líneas más
    abajo: el código tenía la forma correcta y esta función no la aplicaba.

    **Sin `hora_identificacion` se ancla a las 00:00**, que es lo que hacía
    antes. Se descartó caer a `created_at` como proxy: una falla vieja cargada
    meses después arrancaría su SLA en la fecha de carga y podría voltear a
    "Cumplido" algo que no lo fue. Hoy la única fuente que no manda la hora es
    la app móvil; cuando la mande, este caso queda solo para datos legacy.

    Único dueño de la regla: lo usan `limite_sla` y el promedio de resolución de
    `consultas.sla_dashboard`, que antes calculaban la medianoche por separado.
    """
    return datetime.combine(
        falla.fecha_identificacion,
        falla.hora_identificacion or time(0, 0),
        tzinfo=_COL_TZ,
    )


def limite_sla(falla: Falla, sla_hours: int | None = None) -> datetime:
    """El instante en que vence el SLA, en hora Colombia (no UTC)."""
    horas = sla_hours if sla_hours is not None else sla_limite_horas_efectivo(falla)
    return inicio_sla(falla) + timedelta(hours=horas)


def fotos_lista(falla: Falla) -> list:
    """`fotos_urls` como lista, tolerando el formato legado.

    Con JSONB el ORM ya devuelve list o str según cómo se guardó; se manejan
    ambos casos más la doble codificación de datos históricos.
    """
    if not falla.fotos_urls:
        return []
    if isinstance(falla.fotos_urls, list):
        return falla.fotos_urls
    try:
        resultado = json.loads(falla.fotos_urls)
        if isinstance(resultado, list):
            return resultado
        if isinstance(resultado, str):
            interno = json.loads(resultado)
            return interno if isinstance(interno, list) else []
        return []
    except (json.JSONDecodeError, TypeError):
        return []


def dias_abierta(falla: Falla) -> int | None:
    if not falla.fecha_identificacion:
        return None
    fin = falla.fecha_resolucion.date() if falla.fecha_resolucion else hoy_col()
    return max(0, (fin - falla.fecha_identificacion).days)


def sla_limite_dias(falla: Falla) -> int:
    """División entera de `sla_limite_horas_efectivo` por 24.

    Antes se calculaba sobre `sla_limite_horas` crudo, así que quedaba en None
    para el 99.94 % de las fallas sin override (auditoría 2026-09-02).
    """
    return sla_limite_horas_efectivo(falla) // 24


def tiempo_afectacion_horas(falla: Falla, intervalos=None) -> float | None:
    """Tiempo total de afectación, en horas.

    Si la falla tiene intervalos de disparo registrados suma la duración de cada
    uno —los abiertos se cierran provisionalmente con la hora actual—; si no,
    cae al span único ocurrencia → resolución. `None` si no hay forma de
    calcularlo (falla abierta y sin intervalos).

    `intervalos` se puede pasar ya precargado para evitar una consulta por fila.
    """
    def _aware(dt: datetime, ref: datetime | None = None) -> datetime:
        if dt.tzinfo is not None:
            return dt
        return dt.replace(tzinfo=ref.tzinfo if ref and ref.tzinfo else _COL_TZ)

    # 1) Los intervalos de disparo mandan si los hay.
    filas = falla.intervalos.all() if intervalos is None else intervalos
    filas = [iv for iv in filas if iv.inicio]
    if filas:
        ahora = datetime.now(_COL_TZ)
        total_seg = 0.0
        for iv in filas:
            inicio = _aware(iv.inicio)
            fin = _aware(iv.fin, inicio) if iv.fin else ahora
            total_seg += max(0.0, (fin - inicio).total_seconds())
        return round(total_seg / 3600, 2)

    # 2) Respaldo: span único ocurrencia → resolución.
    if not falla.fecha_resolucion:
        return None
    inicio = falla.fecha_ocurrencia
    if inicio is None:
        if not falla.fecha_identificacion:
            return None
        inicio = datetime.combine(
            falla.fecha_identificacion,
            falla.hora_identificacion or time(0, 0),
            tzinfo=_COL_TZ,
        )
    fin = falla.fecha_resolucion
    inicio, fin = _aware(inicio, fin), _aware(fin, inicio)
    return round(max(0.0, (fin - inicio).total_seconds() / 3600), 2)


def estimar_perdida(potencia_kwp, horas_fuera: float) -> tuple[float, float]:
    """`(kWh perdidos, impacto COP)` de una falla.

    `solar_hours` aproxima las horas productivas como ~50 % del downtime (≈12 h
    solares por cada 24). Función pura: alimenta el reporte SLA/económico.
    """
    solar_hours = min(horas_fuera, (horas_fuera / 24) * 12) if horas_fuera > 0 else 0
    kwh_perdidos = (
        round(float(potencia_kwp) * SOLAR_CAPACITY_FACTOR * solar_hours, 3)
        if potencia_kwp else 0.0
    )
    return kwh_perdidos, round(kwh_perdidos * PRECIO_ENERGIA_COP_KWH, 2)


# ── Clasificación estructurada ───────────────────────────────────────────────

def _inv_tipos(inv) -> list:
    if isinstance(inv, dict):
        return inv.get("tipos") or []
    return getattr(inv, "tipos", None) or []


def _inv_get(inv, clave):
    if isinstance(inv, dict):
        return inv.get(clave)
    return getattr(inv, clave, None)


def validar_payload(categoria_codigo, subtipo_codigo, inversores) -> None:
    """Valida el payload estructurado contra la estructura canónica; 422 si no."""
    inv_tipos = sorted({t for inv in (inversores or []) for t in _inv_tipos(inv)})
    ok, err = validar_clasificacion(categoria_codigo, subtipo_codigo, inv_tipos)
    if not ok:
        raise NoProcesable(f"Clasificación inválida: {err}")


def sincronizar_inversores(falla: Falla, inversores: list | None) -> None:
    """Reemplaza los inversores afectados de la falla (replace-all)."""
    if inversores is None:
        return
    FallaInversor.objects.filter(falla_id=falla.id).delete()
    FallaInversor.objects.bulk_create([
        FallaInversor(
            falla_id=falla.id,
            proyecto_inversor_id=_inv_get(inv, "proyecto_inversor_id"),
            nombre=_inv_get(inv, "nombre"),
            potencia_kw=_inv_get(inv, "potencia_kw"),
            tipos=_inv_tipos(inv) or [],
        )
        for inv in inversores
    ])


def sincronizar_intervalos(falla: Falla, intervalos: list | None) -> None:
    """Reemplaza los intervalos de disparo. `None` no toca nada; `[]` los borra."""
    if intervalos is None:
        return
    FallaIntervalo.objects.filter(falla_id=falla.id).delete()
    FallaIntervalo.objects.bulk_create([
        FallaIntervalo(
            falla_id=falla.id,
            inicio=iv["inicio"],
            fin=iv.get("fin"),
            nota=iv.get("nota") or None,
        )
        for iv in intervalos if iv.get("inicio")
    ])


def aplicar_clasificacion(falla: Falla, inversores: list | None) -> None:
    """Deriva `tipo_id`, `pendiente_reclasificar`, las banderas y `clasificacion`
    de las columnas ya asignadas y de la lista de inversores. Sincroniza
    `falla_inversores`.

    Asume que la clasificación ya se validó. Con `inversores=None` (un PATCH que
    no los toca) recalcula a partir de las filas existentes.
    """
    categoria = falla.categoria_codigo
    cat = get_categoria(categoria) if categoria else None
    if not cat:
        return

    if inversores is None and categoria == "inversores":
        inv_source = [
            {"proyecto_inversor_id": e.proyecto_inversor_id, "nombre": e.nombre,
             "potencia_kw": float(e.potencia_kw) if e.potencia_kw is not None else None,
             "tipos": e.tipos or []}
            for e in FallaInversor.objects.filter(falla_id=falla.id)
        ]
    else:
        inv_source = inversores or []

    inv_tipos_all = sorted({t for inv in inv_source for t in _inv_tipos(inv)})

    falla.pendiente_reclasificar = es_subtipo_pendiente(categoria, falla.subtipo_codigo)
    falla.inversores_perdida_comunicacion = (
        ("perdida_comunicacion" in inv_tipos_all) if categoria == "inversores" else None
    )
    if categoria != "frontera":
        falla.frontera_afecta_medicion = None
        falla.frontera_perdida_comunicacion = None

    # Mapeo al tipo del catálogo, para que las vistas legacy muestren su
    # etiqueta. Genérico a propósito: agregar una categoría a ESTRUCTURA_FALLAS
    # no debe exigir tocar esta lista.
    nuevo_tipo_id = None
    if cat["tipo"] in ("opcion", "equipo") and falla.subtipo_codigo:
        t = FallaCatTipo.objects.filter(
            codigo=tipo_codigo(categoria, falla.subtipo_codigo)
        ).first()
        nuevo_tipo_id = t.id if t else None
    elif categoria == "inversores" and inv_tipos_all:
        t = FallaCatTipo.objects.filter(
            codigo=tipo_codigo("inversores", inv_tipos_all[0])
        ).first()
        nuevo_tipo_id = t.id if t else None
    falla.tipo_id = nuevo_tipo_id

    # Snapshot estructurado: fuente para mostrar y auditar, y también para armar
    # el título legible al vuelo (reemplaza a `tipo_libre`, eliminado el
    # 2026-09-02 junto con el backfill permanente que lo mantenía sincronizado).
    clasif = {"categoria": categoria, "categoria_etiqueta": cat["etiqueta"]}
    if falla.subtipo_codigo:
        clasif["subtipo"] = falla.subtipo_codigo
        clasif["subtipo_etiqueta"] = etiqueta_subtipo(categoria, falla.subtipo_codigo)
    if falla.subtipo_detalle:
        clasif["detalle"] = falla.subtipo_detalle
    if categoria == "frontera":
        clasif["afecta_medicion"] = bool(falla.frontera_afecta_medicion)
        clasif["perdida_comunicacion"] = bool(falla.frontera_perdida_comunicacion)
    if categoria == "inversores":
        clasif["inversores"] = [
            {
                "proyecto_inversor_id": _inv_get(inv, "proyecto_inversor_id"),
                "nombre": _inv_get(inv, "nombre"),
                "potencia_kw": _inv_get(inv, "potencia_kw"),
                "tipos": _inv_tipos(inv),
                "tipos_etiquetas": [
                    etiqueta_subtipo("inversores", t) or t for t in _inv_tipos(inv)
                ],
            }
            for inv in inv_source
        ]
    falla.clasificacion = clasif

    sincronizar_inversores(falla, inversores)


# ── Cierre y SLA ─────────────────────────────────────────────────────────────

def es_patron_bot_externo(falla: Falla) -> bool:
    return (
        falla.alarma_monitoreo_id is None
        and falla.categoria_codigo == BOT_DESCONEXION_CATEGORIA
        and falla.subtipo_codigo == BOT_DESCONEXION_SUBTIPO
    )


def bloquear_cierre_si_pendiente(falla: Falla, nuevo_estado) -> None:
    """Impide cerrar una falla que sigue pendiente de reclasificar.

    No aplica al patrón del bot externo: bloquearlo ahí rompería su flujo de
    cierre automático, que no controlamos.
    """
    if not nuevo_estado or not nuevo_estado.es_estado_final:
        return
    if not falla.pendiente_reclasificar:
        return
    if es_patron_bot_externo(falla):
        return
    raise Conflict(
        "Esta falla sigue pendiente de reclasificar (la causa real no está confirmada). "
        "Elegí el subtipo definitivo antes de cerrarla."
    )


def sincronizar_resolucion(falla: Falla, nuevo_estado) -> None:
    """Sella `fecha_resolucion` + `sla_cumplido` al cerrar; los limpia al reabrir."""
    if not nuevo_estado:
        return
    if nuevo_estado.es_estado_final:
        if not falla.fecha_resolucion:
            falla.fecha_resolucion = datetime.now(timezone.utc)
        falla.sla_cumplido = falla.fecha_resolucion <= limite_sla(falla)
    else:
        falla.fecha_resolucion = None
        falla.sla_cumplido = None


def integridad_a_error(exc) -> Exception:
    """Traduce un IntegrityError crudo a un error HTTP legible.

    Antes un FK inexistente volaba hasta el cliente como un 500 de Postgres sin
    mensaje claro — deuda documentada en `docs/API_FALLAS.md` para los
    integradores externos. Se detecta por el TEXTO del mensaje y no por el nombre
    de la constraint, que es lo portable entre motores (auditoría 2026-09-02).
    """
    if "foreign key" in str(exc).lower():
        return NoProcesable(
            "Uno de los IDs enviados (proyecto_id/tipo_id/estado_id/prioridad_id/"
            "resolucion_id) no existe"
        )
    return NoProcesable(
        "No se pudo guardar la falla: violación de integridad en los datos enviados"
    )
