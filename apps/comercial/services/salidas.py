"""La forma de salida del CRM: oferta, oportunidad y planta.

Puerto de los helpers de presentación de `app/api/v1/comercial.py`. No consultan
nada: reciben lo ya cargado y arman el dict. Lo que sí consulta vive en
`consultas.py`.

**Un cliente NO tiene etapa: el negocio es la oferta.** Por eso `_op_base_out`
devuelve `etapas` —el conteo por etapa de sus ofertas— y no un estado único. La
alerta sí se agrega: es la de la oferta más rezagada de las abiertas, para que
una sola oferta olvidada marque al cliente en la lista.
"""

from __future__ import annotations

import enum
import re
from datetime import datetime

from apps.comercial.services.pipeline import (
    calcular_alerta, col_now, operador_red_legal, resumen_etapas,
)
from apps.comun.config import settings

# Código de seguimiento: prefijo estandarizado OF→OP (oferta y oportunidad).
# El segmento de tipo aplica a ofertas NUEVAS; las existentes conservan el suyo
# real (p. ej. 'REPCGM').
_SEG_TIPO = {
    "servicios_operacionales": "REP",
    "compra_energia": "COM",
    "comunidad_energetica": "CEN",
}
_RE_CONSECUTIVO = re.compile(r"No\.\s*(\d+)")

# Días sin respuesta a partir de los cuales una oferta abierta marca alerta.
# Mismo default que el `Settings` de FastAPI (5), no 15.
ALERTA_DIAS = int(settings.COMERCIAL_ALERTA_DIAS or 5)


def norm_codigo(s: str | None) -> str | None:
    """Estandariza el prefijo del código de seguimiento OF→OP. Idempotente."""
    if s and s[:2].upper() == "OF":
        return "OP" + s[2:]
    return s


def valor(v):
    """Enum → slug; deja pasar lo que ya es str puro o None.

    El enum se chequea ANTES que el str: los enums del CRM heredaban de `str`,
    así que con el orden inverso esto devolvía el miembro sin normalizar nada.
    """
    if v is None:
        return None
    return v.value if isinstance(v, enum.Enum) else v


def proyecto_out(p) -> dict:
    return {
        "id": p.id,
        "nombre_comercial": p.nombre_comercial,
        "potencia_ac_kw": (
            float(p.potencia_ac_kw) if p.potencia_ac_kw is not None else None
        ),
        "departamento": p.departamento,
        "municipio": p.municipio,
        "operador_red": operador_red_legal(p),
        "operador_red_id": p.operador_red_id,
        "estado": p.estado,
        "fecha_estimada_energizacion": p.fecha_estimada_energizacion,
        "fecha_inicio_comercializacion": p.fecha_inicio_comercializacion,
    }


def oferta_out(o, ficha: dict | None = None, plantas: list | None = None) -> dict:
    return {
        "id": o.id, "oportunidad_id": o.oportunidad_id,
        "tipo": valor(o.tipo),
        "planta_nombre": o.planta_nombre, "proyecto_id": o.proyecto_id,
        # Todas las plantas de la oferta: es lo que /firmar pasa al contrato.
        "plantas": plantas if plantas is not None else [],
        "numero_oferta": o.numero_oferta,
        "codigo_seguimiento": norm_codigo(o.numero_oferta),
        "precio_detalle": o.precio_detalle,
        # Etapa propia de la oferta. `resultado` se deriva de ella y viaja solo
        # para que no se rompa lo que ya lo leía.
        "estado": valor(o.estado),
        "estado_desde": o.estado_desde,
        "resultado": valor(o.resultado),
        "etapa_texto": o.etapa_texto, "fecha_oferta": o.fecha_oferta,
        "fecha_tentativa_inicio": o.fecha_tentativa_inicio,
        # Fin tentativo del suministro: con el inicio, es el período que declara
        # un PPA todavía en borrador.
        "fecha_fin_tentativa": o.fecha_fin_tentativa,
        "contrato_firmado": o.contrato_firmado, "detalle": o.detalle, "notas": o.notas,
        "seguimientos": o.seguimientos or 0,
        "fecha_ultima_respuesta": o.fecha_ultima_respuesta,
        "documento_url": o.documento_url,
        # En qué contrato desembocó. Las condiciones viven allá, no acá.
        "ppa_contrato_id": o.ppa_contrato_id,
        "contrato_servicio_id": o.contrato_servicio_id,
        # Lo DECLARADO en la oferta, en crudo: el editor necesita distinguirlo de
        # lo resuelto en `ficha`, que puede venir del Proyecto.
        "municipio": o.municipio,
        "departamento": o.departamento,
        "operador_red_id": o.operador_red_id,
        "energia_promedio_kwh_mes": (
            float(o.energia_promedio_kwh_mes)
            if o.energia_promedio_kwh_mes is not None else None
        ),
        # Los 6 parámetros resueltos por cascada, con el origen de cada uno.
        "ficha": ficha,
        "created_at": o.created_at, "updated_at": o.updated_at,
    }


def resumen_ofertas(ofertas) -> dict:
    """Conteo por tipo, p. ej. `{'servicios_operacionales': 3, 'compra_energia': 1}`."""
    salida: dict = {}
    for o in ofertas:
        t = valor(o.tipo)
        salida[t] = salida.get(t, 0) + 1
    return salida


def op_base_out(op, cliente, ultima_gestion, ahora: datetime,
                ofertas_estado: list[tuple] | None = None) -> dict:
    """`ofertas_estado` es `[(estado, estado_desde), …]` de las ofertas del cliente."""
    dias, alerta = 0, False
    for estado, desde in (ofertas_estado or []):
        d, a = calcular_alerta(
            estado, desde or op.estado_desde, ultima_gestion, ALERTA_DIAS, ahora
        )
        if a and (not alerta or d > dias):
            dias, alerta = d, True
        elif not alerta and d > dias:
            dias = d
    return {
        "id": op.id,
        "etapas": resumen_etapas(e for e, _ in (ofertas_estado or [])),
        "nombre": op.nombre or cliente.razon_social_nombre,
        "cliente_id": op.cliente_id,
        "cliente_razon_social": cliente.razon_social_nombre,
        "cliente_nit": cliente.nit_cedula,
        "numero_oferta": op.numero_oferta,
        "es_migrada": op.es_migrada,
        "dias_sin_respuesta": dias,
        "alerta": alerta,
        "ultima_gestion_fecha": ultima_gestion,
        "created_at": op.created_at,
        "updated_at": op.updated_at,
    }
