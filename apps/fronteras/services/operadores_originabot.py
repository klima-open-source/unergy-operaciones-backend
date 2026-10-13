"""Unión del catálogo de operadores de red con el de originabot (TSF).

originabot tiene su propio catálogo (`grid_operator_request.GridOperator`), más
completo que el nuestro, y cada operador trae un `code` estable (`afinia`,
`aire`, `essa`…). `OperadorRed.codigo_originabot` guarda ese code: con él,
importar un proyecto de TSF mapea su operador sin comparar nombres.

`vincular()` es la única puerta, y es idempotente:

1. si ya hay un operador con ese code, es ese;
2. si no, uno SIN code cuyo nombre (comercial o legal, normalizado) coincida se
   queda con el code — así se unen los que ya existían;
3. si tampoco, se crea. originabot no publica la razón social, así que el
   nombre legal nace igual al comercial y se corrige a mano después.

El paso 2 solo mira operadores sin code a propósito: originabot tiene `ceo` y
`ceo esp`, que al normalizar se ven iguales; sin esa condición el segundo le
robaría la fila al primero.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction

from apps.comun.nombre_matching import normalizar
from apps.fronteras.models import OperadorRed

EXISTENTE = "existente"
VINCULADO = "vinculado"
CREADO = "creado"


class VinculoImposible(Exception):
    """El code es nuevo pero su nombre ya es el nombre legal de otro operador
    que tiene OTRO code: crearlo chocaría con el UNIQUE de `nombre_legal`."""


def _por_nombre_sin_codigo(nombre: str) -> OperadorRed | None:
    clave = normalizar(nombre)
    if not clave:
        return None
    for operador in OperadorRed.objects.filter(codigo_originabot__isnull=True).order_by("id"):
        if clave in (normalizar(operador.nombre_comercial or ""), normalizar(operador.nombre_legal)):
            return operador
    return None


def que_pasaria(codigo: str, nombre: str) -> tuple[OperadorRed | None, str]:
    """Lo que haría `vincular()`, sin escribir: para que quien la llama pueda
    pedir confirmación antes de CREAR un operador."""
    codigo = (codigo or "").strip().lower()
    nombre = (nombre or "").strip()
    if not codigo or not nombre:
        raise ValueError("Hacen falta el código y el nombre del operador de originabot.")
    existente = OperadorRed.objects.filter(codigo_originabot=codigo).first()
    if existente:
        return existente, EXISTENTE
    candidato = _por_nombre_sin_codigo(nombre)
    if candidato:
        return candidato, VINCULADO
    if OperadorRed.objects.filter(nombre_legal=nombre).exists():
        raise VinculoImposible(
            f"Ya existe un operador con nombre legal «{nombre}» y otro código de originabot."
        )
    return None, CREADO


def vincular(codigo: str, nombre: str) -> tuple[OperadorRed, str]:
    """El operador de red de Operaciones para el `code` de originabot.

    Devuelve el operador y qué pasó: `existente`, `vinculado` o `creado`.
    """
    codigo = (codigo or "").strip().lower()
    nombre = (nombre or "").strip()
    if not codigo or not nombre:
        raise ValueError("Hacen falta el código y el nombre del operador de originabot.")

    existente = OperadorRed.objects.filter(codigo_originabot=codigo).first()
    if existente:
        return existente, EXISTENTE

    try:
        with transaction.atomic():
            candidato = _por_nombre_sin_codigo(nombre)
            if candidato:
                candidato.codigo_originabot = codigo
                candidato.save(update_fields=["codigo_originabot", "updated_at"])
                return candidato, VINCULADO
            if OperadorRed.objects.filter(nombre_legal=nombre).exists():
                raise VinculoImposible(
                    f"Ya existe un operador con nombre legal «{nombre}» y otro código de "
                    f"originabot; revisa a mano antes de vincular «{codigo}»."
                )
            return (
                OperadorRed.objects.create(
                    nombre_legal=nombre, nombre_comercial=nombre, codigo_originabot=codigo,
                ),
                CREADO,
            )
    except IntegrityError:
        # Dos importaciones simultáneas del mismo operador: la otra ganó el
        # UNIQUE de `codigo_originabot`, y su fila es la buena.
        ganador = OperadorRed.objects.filter(codigo_originabot=codigo).first()
        if ganador is None:
            raise
        return ganador, EXISTENTE
