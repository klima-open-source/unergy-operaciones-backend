"""Llena la tarifa de cada servicio (base, índice y `tarifas`) de los contratos que ya existían.

**Por defecto solo lee.** Escribe únicamente con `--aplicar`.

    python manage.py registrar_tarifas
    python manage.py registrar_tarifas --aplicar

Para qué. `tarifas` nace vacía y `servicios` sin base (`contratos/0019`). Desde ese
deploy `Contrato.save()` y las escrituras de `ppa_tarifas` las derivan
(`services/tarifas.py`); este comando hace lo mismo UNA vez para los que ya estaban,
PPA incluidos. Dice, por servicio, cuántos quedaron con base, cuántos con filas, y
qué contratos con precio no tienen dónde ponerlo (un PPA sin servicio de compra ni
de venta).

Es idempotente. Con o sin `--aplicar` hace el mismo trabajo dentro de una
transacción; sin `--aplicar` la deshace al final.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Q

from apps.contratos.models import Contrato, GrupoContrato, Servicio, Tarifa
from apps.contratos.services import tarifas
from apps.ppa.models import PpaTarifa


class Command(BaseCommand):
    help = "Llena la base, el índice y las tarifas de cada servicio (solo lee sin --aplicar)."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Escribe los cambios.")

    def handle(self, *args, aplicar=False, **opciones):
        contratos = Contrato.objects.order_by("id")
        antes = Tarifa.objects.count()
        cambiados = 0
        with transaction.atomic():
            for contrato in contratos:
                cambiados += tarifas.sincronizar(contrato)
            por_servicio = list(
                Servicio.objects.values("servicio")
                .annotate(
                    servicios=Count("id", distinct=True),
                    con_base=Count("id", filter=Q(tarifa_base__isnull=False), distinct=True),
                    con_filas=Count("id", filter=Q(tarifas__isnull=False), distinct=True),
                    filas=Count("tarifas"),
                )
                .order_by("servicio")
            )
            despues = Tarifa.objects.count()
            ppa_sin_servicio = list(
                Contrato.objects.filter(grupo=GrupoContrato.PPA)
                .filter(Q(tarifa_base__isnull=False)
                        | Q(pk__in=PpaTarifa.objects.values("contrato_id")))
                .exclude(servicios__servicio__in=tarifas.SERVICIOS_PPA)
                .values_list("id", flat=True)
            )
            if not aplicar:
                transaction.set_rollback(True)

        self.stdout.write(f"Contratos: {contratos.count()} ({cambiados} con cambios). "
                          f"Filas en tarifas: {antes} antes, {despues} después.")
        for fila in por_servicio:
            self.stdout.write(
                f"  {fila['servicio']:<15} {fila['servicios']:>4} servicios, "
                f"{fila['con_base']:>4} con base, {fila['con_filas']:>4} con filas "
                f"({fila['filas']} filas, {tarifas.UNIDAD.get(fila['servicio'], '?')})"
            )
        if ppa_sin_servicio:
            self.stdout.write(self.style.WARNING(
                f"PPA con precio y sin servicio de compra ni venta (su precio no se copia): "
                f"{ppa_sin_servicio}"
            ))
        if not aplicar:
            self.stdout.write("Solo lectura: corre con --aplicar para escribir.")
