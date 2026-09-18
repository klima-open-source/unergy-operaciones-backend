"""Backfill de `modalidad_pago='plc'` para los contratos PLC (Pague lo Contratado).

El campo `asic_solicitudes.modalidad_pago` existe pero está vacío en todas las
filas, y es lo que la atribución de garantía usa para saber qué contratos generan
garantía (los PLC + los duplicados). Esta lista la confirmó Jessica (2026-09-18).
Nada calcula sobre `modalidad_pago` hoy salvo la atribución, así que llenarlo no
mueve ningún otro número.

Correr una vez en el servidor:
    docker compose exec operaciones python manage.py backfill_modalidad_plc
    docker compose exec operaciones python manage.py backfill_modalidad_plc --commit
Sin `--commit` solo muestra qué cambiaría (dry-run).
"""

from django.core.management.base import BaseCommand

# SIC de contratos PLC (por código de contrato en asic_solicitudes).
SICS_PLC = [
    "86512", "86924",                               # NEU I, NEU 2
    "87551", "88749", "87552", "88751", "87553",    # BIA (Delta 1, Naos 1/2/3, Polaris 1)
    "89900", "89901", "89902",                      # Terpel 8
    "88747", "88748",                               # Nitro (2 de 4)
]


class Command(BaseCommand):
    help = "Marca modalidad_pago='plc' en las filas asic de los contratos PLC."

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit", action="store_true",
            help="Aplica los cambios. Sin esto es un dry-run.",
        )

    def handle(self, *args, **opts):
        from apps.mercado_xm.models import AsicSolicitud

        qs = AsicSolicitud.objects.filter(codigo_sic_contrato__in=SICS_PLC)
        total = qs.count()
        ya_plc = qs.filter(modalidad_pago="plc").count()
        por_cambiar = qs.exclude(modalidad_pago="plc")
        n = por_cambiar.count()

        presentes = set(qs.values_list("codigo_sic_contrato", flat=True))
        faltantes = [s for s in SICS_PLC if s not in presentes]

        self.stdout.write(f"SIC PLC objetivo: {len(SICS_PLC)}")
        self.stdout.write(f"Filas asic encontradas: {total} (ya en plc: {ya_plc})")
        self.stdout.write(f"Filas a marcar plc: {n}")
        if faltantes:
            self.stdout.write(self.style.WARNING(
                f"SIN filas en asic (revisar): {', '.join(faltantes)}"
            ))

        if not opts["commit"]:
            self.stdout.write(self.style.NOTICE("DRY-RUN (usa --commit para aplicar)."))
            return

        actualizadas = por_cambiar.update(modalidad_pago="plc")
        self.stdout.write(self.style.SUCCESS(f"Listo: {actualizadas} filas marcadas plc."))
