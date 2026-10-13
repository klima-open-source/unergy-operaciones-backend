"""Clientes de Operaciones unidos con los propietarios (`LandLord`) de originabot.

«Sincronizar con TSF» trae los contratos de arrendamiento de la planta con su
lista de arrendadores; cada uno se vincula con un cliente de Operaciones (la
factura del arriendo sale a su nombre y NIT). Mismo patrón que
`inversionistas_originabot`, pero con el documento como segunda llave:

1. un cliente con ese `originabot_landlord_id` es ese;
2. si no, uno SIN vínculo con el mismo documento — solo si TSF lo da completo
   (en dev llega enmascarado, «19****53», y ahí no sirve);
3. si no, uno SIN vínculo con el mismo nombre normalizado, si es el único;
4. si tampoco, se crea con el nombre, el documento (si viene completo) y el tipo
   de persona.

`que_pasaria()` dice lo que haría `vincular()` sin escribir, para que la pantalla
pida confirmación antes de CREAR un cliente.
"""
from __future__ import annotations

import re

from django.db import IntegrityError, transaction

from apps.clientes.models import Cliente
from apps.comun.nombre_matching import normalizar

EXISTENTE = "existente"
VINCULADO = "vinculado"
CREADO = "creado"


def documento_util(documento: str | None) -> str | None:
    """El documento sin separadores, o None si viene enmascarado o vacío."""
    if not documento or "*" in documento:
        return None
    limpio = re.sub(r"[^0-9A-Za-z]", "", documento)
    return limpio or None


def _vivos():
    return Cliente.objects.filter(deleted_at__isnull=True)


def _limpiar(landlord_id, nombre) -> tuple[int, str]:
    nombre = (nombre or "").strip()
    if not isinstance(landlord_id, int) or landlord_id <= 0 or not nombre:
        raise ValueError("Hacen falta el id y el nombre del arrendador de originabot.")
    return landlord_id, nombre


def _candidato(nombre: str, documento: str | None) -> Cliente | None:
    sin_vinculo = _vivos().filter(originabot_landlord_id__isnull=True)
    if documento:
        por_doc = [c for c in sin_vinculo if documento_util(c.nit_cedula) == documento]
        if len(por_doc) == 1:
            return por_doc[0]
    clave = normalizar(nombre)
    por_nombre = [c for c in sin_vinculo if clave and normalizar(c.razon_social_nombre) == clave]
    return por_nombre[0] if len(por_nombre) == 1 else None


def que_pasaria(landlord_id, nombre, documento=None) -> tuple[Cliente | None, str]:
    landlord_id, nombre = _limpiar(landlord_id, nombre)
    existente = _vivos().filter(originabot_landlord_id=landlord_id).first()
    if existente:
        return existente, EXISTENTE
    candidato = _candidato(nombre, documento_util(documento))
    if candidato:
        return candidato, VINCULADO
    return None, CREADO


def vincular(landlord_id, nombre, documento=None, tipo_persona=None) -> tuple[Cliente, str]:
    """El cliente de Operaciones para un arrendador de originabot. Idempotente."""
    landlord_id, nombre = _limpiar(landlord_id, nombre)
    try:
        with transaction.atomic():
            cliente, estado = que_pasaria(landlord_id, nombre, documento)
            if estado == EXISTENTE:
                return cliente, estado
            if estado == VINCULADO:
                Cliente.objects.filter(pk=cliente.pk).update(originabot_landlord_id=landlord_id)
                cliente.originabot_landlord_id = landlord_id
                return cliente, estado
            # El NIT es UNIQUE: si ya lo tiene otro cliente (vinculado a otro
            # arrendador), el nuevo nace sin él y se revisa a mano.
            nit = documento_util(documento)
            if nit and Cliente.objects.filter(nit_cedula=nit).exists():
                nit = None
            return (
                Cliente.objects.create(
                    razon_social_nombre=nombre,
                    nit_cedula=nit,
                    tipo_persona=tipo_persona if tipo_persona in ("natural", "juridica") else None,
                    originabot_landlord_id=landlord_id,
                    origen_tipo="originabot",
                    origen_detalle=f"Arrendador {landlord_id} de TSF",
                ),
                CREADO,
            )
    except IntegrityError:
        # Otra sincronización ganó el UNIQUE (de la llave o del NIT): su cliente es el bueno.
        ganador = _vivos().filter(originabot_landlord_id=landlord_id).first()
        if ganador is None:
            raise
        return ganador, EXISTENTE
