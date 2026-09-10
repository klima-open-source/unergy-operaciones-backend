"""Deja los `clientes.nit_cedula` con solo sus dígitos.

**Por qué hace falta.** El UNIQUE de `nit_cedula` compara texto, así que
"900.123.456-7", "900123456-7" y "9001234567" son tres valores distintos: el
mismo cliente podía entrar tres veces sin que nada avisara. El serializer ya
guarda solo los dígitos (`_NitNormalizado` en `api/v1/clientes/serializers.py`),
pero eso solo arregla lo que se escriba de ahora en adelante: mientras las filas
viejas conserven su formato, un alta nueva no choca con ellas y la protección no
sirve para lo que ya está.

**Se corre una vez, a mano, y se borra** (regla de CLAUDE.md: los datos no se
migran dentro de una migración de Django). En el servidor:

    docker compose exec operaciones python manage.py normalizar_nit_clientes
    docker compose exec operaciones python manage.py normalizar_nit_clientes --ejecutar

**Por defecto no escribe.** La primera corrida es un reporte, y lo importante
del reporte no son los cambios de formato: son las COLISIONES. Dos clientes
cuyos NIT normalizan al mismo valor son, casi seguro, el mismo cliente cargado
dos veces -- y no se pueden normalizar los dos porque el UNIQUE lo impide. Esas
filas se dejan intactas y se listan: qué hacer con ellas (fusionar, corregir, dar
de baja una) es una decisión de negocio, no algo que este comando deba adivinar.
"""
import re
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.clientes.models import Cliente


def solo_digitos(valor: str | None) -> str | None:
    digitos = re.sub(r"\D", "", valor or "")
    return digitos or None


class Command(BaseCommand):
    help = "Normaliza clientes.nit_cedula a solo dígitos (reporte por defecto)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--ejecutar", action="store_true",
            help="Escribe los cambios. Sin esto solo reporta.",
        )

    def handle(self, *args, **opciones):
        ejecutar = opciones["ejecutar"]

        # Incluye los borrados a propósito: siguen ocupando su NIT en el UNIQUE,
        # así que si no se normalizan, un alta nueva puede chocar con una fila
        # que nadie ve en el listado.
        filas = list(
            Cliente.objects.exclude(nit_cedula__isnull=True)
            .exclude(nit_cedula="")
            .order_by("id")
            .values_list("id", "razon_social_nombre", "nit_cedula")
        )

        por_normalizado: dict[str, list[tuple]] = defaultdict(list)
        for cid, nombre, nit in filas:
            normalizado = solo_digitos(nit)
            if normalizado:
                por_normalizado[normalizado].append((cid, nombre, nit))

        colisiones = {k: v for k, v in por_normalizado.items() if len(v) > 1}
        # Solo lo que de verdad cambia, y solo si no choca con nadie.
        cambios = [
            (cid, nombre, nit, normalizado)
            for normalizado, grupo in por_normalizado.items()
            if normalizado not in colisiones
            for cid, nombre, nit in grupo
            if nit != normalizado
        ]

        self.stdout.write(f"Clientes con NIT: {len(filas)}")
        self.stdout.write(f"Por normalizar:   {len(cambios)}")
        self.stdout.write(f"Colisiones:       {len(colisiones)} grupo(s)\n")

        for cid, nombre, antes, despues in cambios:
            self.stdout.write(f"  #{cid} {nombre}: {antes!r} -> {despues!r}")

        if colisiones:
            self.stdout.write(self.style.WARNING(
                "\nEstos grupos normalizan al MISMO NIT y se dejan sin tocar. "
                "Casi seguro es el mismo cliente cargado dos veces; hay que "
                "decidir a mano (fusionar con POST /clientes/{id}/merge/{otro}, "
                "corregir el dato, o dar de baja una de las filas):"
            ))
            for normalizado, grupo in sorted(colisiones.items()):
                self.stdout.write(f"\n  {normalizado}:")
                for cid, nombre, nit in grupo:
                    self.stdout.write(f"    #{cid} {nombre} ({nit!r})")

        if not ejecutar:
            self.stdout.write(self.style.NOTICE(
                "\nNada escrito. Repetí con --ejecutar cuando el reporte esté revisado."
            ))
            return

        if not cambios:
            self.stdout.write("\nNada que escribir.")
            return

        with transaction.atomic():
            for cid, _nombre, _antes, despues in cambios:
                Cliente.objects.filter(pk=cid).update(nit_cedula=despues)

        self.stdout.write(self.style.SUCCESS(f"\n{len(cambios)} NIT normalizado(s)."))
