"""Crea los inversionistas de las participaciones que ya existían y las vincula.

**Por defecto solo lee.** Escribe únicamente con `--aplicar`.

    python manage.py poblar_inversionistas
    python manage.py poblar_inversionistas --aplicar

Para qué. `inversionistas` nace vacía (`proyectos/0009`): una fila por cliente que
es inversionista de alguna planta. Desde ese deploy `ProyectoInversionista.save()`
la mantiene al registrar o editar una participación, pero las que ya estaban en
`proyecto_inversionistas` no pasan por ahí. Este comando hace esa parte, una vez:

1. crea el inversionista de cada cliente que tenga participaciones y no lo tenga;
2. llena `proyecto_inversionistas.inversionista_id` donde esté vacío.

Es idempotente: correrlo de nuevo no crea nada si ya está todo vinculado.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.proyectos.models import Inversionista, ProyectoInversionista


class Command(BaseCommand):
    help = "Crea los inversionistas de las participaciones existentes y las vincula (solo lee sin --aplicar)."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Escribe los cambios.")

    def handle(self, *args, aplicar=False, **opciones):
        sin_vincular = ProyectoInversionista.objects.filter(inversionista__isnull=True)
        clientes = set(sin_vincular.values_list("cliente_id", flat=True))
        ya_son = set(
            Inversionista.objects.filter(cliente_id__in=clientes).values_list("cliente_id", flat=True)
        )
        nuevos = sorted(clientes - ya_son)

        self.stdout.write(
            f"Participaciones sin inversionista: {sin_vincular.count()} "
            f"({len(clientes)} clientes; {len(nuevos)} inversionistas por crear)."
        )
        if not aplicar:
            self.stdout.write("Solo lectura: corre con --aplicar para escribir.")
            return

        with transaction.atomic():
            Inversionista.objects.bulk_create(Inversionista(cliente_id=c) for c in nuevos)
            por_cliente = dict(
                Inversionista.objects.filter(cliente_id__in=clientes).values_list("cliente_id", "id")
            )
            vinculadas = 0
            for cliente_id, inversionista_id in por_cliente.items():
                vinculadas += sin_vincular.filter(cliente_id=cliente_id).update(
                    inversionista_id=inversionista_id
                )
        self.stdout.write(f"Creados {len(nuevos)} inversionistas; vinculadas {vinculadas} participaciones.")
