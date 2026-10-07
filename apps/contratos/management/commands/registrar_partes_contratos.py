"""Llena `contrato_partes` desde las columnas de cada contrato, y dice si cuadran.

**Por defecto solo lee.** Escribe únicamente con `--aplicar`.

    python manage.py registrar_partes_contratos
    python manage.py registrar_partes_contratos --aplicar

Para qué. `contrato_partes` nace vacía en la migración `contratos/0012`, y desde ese
deploy cada contrato que se guarda deja sus partes al día (`Contrato.save()`). Los que
no se toquen siguen sin filas: esto los pone al día de una vez, con la misma regla
(`services/contrato_partes.registrar`), así que no hay dos definiciones.

Sin `--aplicar` es también la verificación: compara lo que dicen las columnas
comprador/vendedor/contratante/prestador con lo que hay en la tabla, contrato por
contrato. Después de aplicar, tiene que dar cero diferencias. Se puede repetir.

No toca las columnas: solo escribe la tabla.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.contratos.models import Contrato, ContratoParte
from apps.contratos.services import contrato_partes


class Command(BaseCommand):
    help = "Llena contrato_partes desde las columnas de cada contrato (solo lee sin --aplicar)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--aplicar", action="store_true",
            help="Escribe las filas que faltan y borra las que sobran. Sin esto, solo informa.",
        )

    def handle(self, *args, aplicar=False, **opts):
        en_tabla: dict[int, set] = {}
        for contrato_id, rol, cliente_id in ContratoParte.objects.values_list(
            "contrato_id", "rol", "cliente_id"
        ):
            en_tabla.setdefault(contrato_id, set()).add((rol, cliente_id))

        contratos = list(Contrato.objects.only(
            "id", "grupo", "comprador_id", "vendedor_id", "contratante_id", "prestador_id",
        ).order_by("id"))
        con_diferencias = []
        faltan = sobran = 0
        for c in contratos:
            objetivo = contrato_partes.deseadas(c)
            actuales = en_tabla.get(c.pk, set())
            if objetivo != actuales:
                con_diferencias.append(c)
                faltan += len(objetivo - actuales)
                sobran += len(actuales - objetivo)

        total = sum(len(contrato_partes.deseadas(c)) for c in contratos)
        out = self.stdout.write
        out("Partes de contratos")
        out(f"  Contratos                       : {len(contratos)}")
        out(f"  Partes según las columnas       : {total}")
        out(f"  Contratos con diferencias       : {len(con_diferencias)}")
        out(f"  Filas que faltan en la tabla    : {faltan}")
        out(f"  Filas que sobran en la tabla    : {sobran}")

        if not con_diferencias:
            out(self.style.SUCCESS("  La tabla cuadra con las columnas."))
            return
        if not aplicar:
            out("  Simulación: no se escribió nada. Repite con --aplicar.")
            return

        with transaction.atomic():
            for c in con_diferencias:
                contrato_partes.registrar(c)
        out(self.style.SUCCESS(
            f"  Escritas las partes de {len(con_diferencias)} contratos. "
            "Repite sin --aplicar para verificar: debe dar cero diferencias."
        ))
