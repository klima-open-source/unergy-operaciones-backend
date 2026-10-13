"""Clientes de Operaciones unidos con los inversionistas de originabot (TSF).

La minigranja de TSF trae sus inversionistas (`investment.Investment`: id y
nombre). «Importar desde TSF» los vincula con un cliente de Operaciones, para que
el inversionista se mapee por llave y los porcentajes se configuren aquí.
Mismo patrón que `apps/fronteras/services/operadores_originabot.py`:

1. un cliente con ese `originabot_investment_id` es ese;
2. si no, uno SIN vínculo cuyo nombre (normalizado) coincida se queda con el id;
3. si tampoco, se crea. TSF no publica NIT ni datos fiscales: el cliente nace solo
   con su nombre y se completa a mano.

`que_pasaria()` dice lo que haría `vincular()` sin escribir, para que la pantalla
pida confirmación antes de CREAR un cliente.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction

from apps.clientes.models import Cliente
from apps.comun.nombre_matching import normalizar

EXISTENTE = "existente"
VINCULADO = "vinculado"
CREADO = "creado"


def _limpiar(investment_id, nombre) -> tuple[int, str]:
    nombre = (nombre or "").strip()
    if not isinstance(investment_id, int) or investment_id <= 0 or not nombre:
        raise ValueError("Hacen falta el id y el nombre del inversionista de originabot.")
    return investment_id, nombre


def _vivos():
    return Cliente.objects.filter(deleted_at__isnull=True)


def _por_nombre_sin_vinculo(nombre: str) -> Cliente | None:
    clave = normalizar(nombre)
    if not clave:
        return None
    candidatos = [
        c for c in _vivos().filter(originabot_investment_id__isnull=True).order_by("id")
        if normalizar(c.razon_social_nombre) == clave
    ]
    # Dos clientes vivos con el mismo nombre: no se adivina cuál es el bueno.
    return candidatos[0] if len(candidatos) == 1 else None


def que_pasaria(investment_id, nombre) -> tuple[Cliente | None, str]:
    investment_id, nombre = _limpiar(investment_id, nombre)
    existente = _vivos().filter(originabot_investment_id=investment_id).first()
    if existente:
        return existente, EXISTENTE
    candidato = _por_nombre_sin_vinculo(nombre)
    if candidato:
        return candidato, VINCULADO
    return None, CREADO


def vincular(investment_id, nombre) -> tuple[Cliente, str]:
    """El cliente de Operaciones para un inversionista de originabot. Idempotente."""
    investment_id, nombre = _limpiar(investment_id, nombre)
    try:
        with transaction.atomic():
            cliente, estado = que_pasaria(investment_id, nombre)
            if estado == EXISTENTE:
                return cliente, estado
            if estado == VINCULADO:
                Cliente.objects.filter(pk=cliente.pk).update(originabot_investment_id=investment_id)
                cliente.originabot_investment_id = investment_id
                return cliente, estado
            return (
                Cliente.objects.create(
                    razon_social_nombre=nombre,
                    originabot_investment_id=investment_id,
                    origen_tipo="originabot",
                    origen_detalle=f"Inversionista {investment_id} de TSF",
                ),
                CREADO,
            )
    except IntegrityError:
        # Dos importaciones simultáneas: la otra ganó el UNIQUE; su cliente es el bueno.
        ganador = _vivos().filter(originabot_investment_id=investment_id).first()
        if ganador is None:
            raise
        return ganador, EXISTENTE
