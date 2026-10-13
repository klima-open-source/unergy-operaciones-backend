"""Llena `contrato_proyectos` para los contratos de servicio que ya existían.

**Por defecto solo lee.** Escribe únicamente con `--aplicar`.

    python manage.py registrar_proyectos_contratos
    python manage.py registrar_proyectos_contratos --aplicar
    python manage.py registrar_proyectos_contratos --csv pendientes.csv

Para qué. Desde `contratos/0017`, `contrato_proyectos` es para todos los contratos
y, en representación, dice a qué participación (inversionista de la planta)
representamos. `Contrato.save()` la mantiene al guardar; este comando hace lo mismo
UNA vez para los contratos que ya estaban, con la misma regla
(`services/contrato_proyectos.py`), y dice qué quedó sin resolver.

Pendientes de representación, por motivo:

- `planta sin participaciones`: hay que cargar los inversionistas de la planta.
- `varios inversionistas, el contrato no dice cuál`: hay que escoger.
- `el inversionista del contrato no participa en la planta`: el contrato dice un
  inversionista que no está entre los de la planta.

Es idempotente. Con o sin `--aplicar` hace el mismo trabajo dentro de una
transacción; sin `--aplicar` la deshace al final.
"""
import csv

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.contratos.models import Contrato, ContratoProyecto, GrupoContrato
from apps.contratos.services import contrato_proyectos

CABECERA = ["contrato_id", "numero_contrato", "proyecto_id", "proyecto", "inversionista_contrato", "motivo"]


def _motivo(contrato) -> str:
    from apps.proyectos.models import ProyectoInversionista

    n = ProyectoInversionista.objects.filter(proyecto_id=contrato.proyecto_id).count()
    if n == 0:
        return "planta sin participaciones"
    if not contrato.inversionista_id:
        return "varios inversionistas, el contrato no dice cuál"
    return "el inversionista del contrato no participa en la planta"


class Command(BaseCommand):
    help = "Llena contrato_proyectos para los contratos de servicio (solo lee sin --aplicar)."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Escribe los cambios.")
        parser.add_argument("--csv", dest="archivo", help="Escribe los pendientes en este CSV.")

    def handle(self, *args, aplicar=False, archivo=None, **opciones):
        servicio = (Contrato.objects.exclude(grupo=GrupoContrato.PPA)
                    .select_related("proyecto", "inversionista").order_by("id"))
        antes = ContratoProyecto.objects.exclude(contrato__grupo=GrupoContrato.PPA).count()

        with transaction.atomic():
            for contrato in servicio:
                contrato_proyectos.sincronizar(contrato)

            filas = ContratoProyecto.objects.exclude(contrato__grupo=GrupoContrato.PPA)
            repr_ = filas.filter(contrato__grupo=GrupoContrato.REPRESENTACION_CGM)
            n_repr = repr_.count()
            con_part = repr_.filter(proyecto_inversionista__isnull=False).count()
            pendientes = [
                c for c in servicio.filter(grupo=GrupoContrato.REPRESENTACION_CGM, proyecto__isnull=False)
                if not repr_.filter(contrato_id=c.pk, proyecto_inversionista__isnull=False).exists()
            ]
            sin_planta = servicio.filter(proyecto__isnull=True).count()
            despues = filas.count()
            if not aplicar:
                transaction.set_rollback(True)

        self.stdout.write(
            f"Contratos de servicio: {servicio.count()} ({sin_planta} sin planta). "
            f"Filas en contrato_proyectos: {antes} antes, {despues} después."
        )
        self.stdout.write(
            f"Representación: {n_repr} con planta; "
            f"{con_part} con su participación, {len(pendientes)} pendientes."
        )
        por_motivo = {}
        salida = []
        for c in pendientes:
            motivo = _motivo(c)
            por_motivo[motivo] = por_motivo.get(motivo, 0) + 1
            salida.append([c.pk, c.numero_contrato or "", c.proyecto_id,
                           c.proyecto.nombre_comercial if c.proyecto else "",
                           c.inversionista.razon_social_nombre if c.inversionista else "", motivo])
        for motivo, n in sorted(por_motivo.items()):
            self.stdout.write(f"  {motivo}: {n}")

        if archivo:
            with open(archivo, "w", newline="", encoding="utf-8") as f:
                escritor = csv.writer(f)
                escritor.writerow(CABECERA)
                escritor.writerows(salida)
            self.stdout.write(f"Pendientes escritos en {archivo}.")
        else:
            for fila in salida:
                self.stdout.write("  - " + " | ".join(str(x) for x in fila))
        if not aplicar:
            self.stdout.write("Solo lectura: corre con --aplicar para escribir.")
