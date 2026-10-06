"""Mide cuánto se desvía el costo de representación al elegir un solo contrato.

**Solo lee.** No escribe nada.

    python manage.py revisar_tarifas_por_inversionista

Para qué. En las minigranjas hay **un contrato de representación por
inversionista** (confirmado con negocio el 2026-09-18), y sus tarifas pueden
diferir. Pero `costos.elegir_contrato_representacion` toma UNO solo --se escribió
creyendo que los contratos múltiples eran duplicados del seed-- y el panel
contable reparte ese costo por participación.

Si las tarifas difieren, el costo de cada inversionista queda mal y el total de
la planta también. Este informe dice en cuántas plantas pasa y de qué tamaño es
la desviación, para decidir el arreglo con números en vez de a ojo.

Lo que compara, por planta:

- **hoy**: la tarifa del contrato elegido, aplicada a toda la energía
- **por inversionista**: la tarifa de cada contrato, ponderada por su
  participación vigente

En los proyectos GD no debería salir nada: ahí hay un único contrato.

Ojo con lo que NO mide: el efecto en pesos depende de la energía del mes, que
sale del ER. Acá se compara la tarifa efectiva, que es la parte que el contrato
decide.
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.contabilidad.services.costos import elegir_contrato_representacion
from apps.contratos.models import ContratoServicio
from apps.contratos.services import grupos, vigencia
from apps.plataforma.services.fechas import hoy_col
from apps.proyectos.models import Proyecto, ProyectoInversionista


class Command(BaseCommand):
    help = "Compara el costo de representación por planta contra el de cada inversionista."

    def handle(self, *args, **opciones):
        hoy = hoy_col()
        plantas = {p.id: p for p in Proyecto.objects.filter(deleted_at__isnull=True)}

        # Contratos VIVOS que cubren representación, por planta.
        por_planta: dict[int, list] = {}
        contratos = ContratoServicio.objects.filter(
            vigencia.filtro_vivos(hoy),
            grupos.filtro_subservicio(grupos.REPRESENTACION),
            proyecto__isnull=False,
        )
        for c in contratos:
            if c.proyecto_id in plantas:
                por_planta.setdefault(c.proyecto_id, []).append(c)

        # Participación vigente de cada inversionista, por nombre normalizado:
        # los contratos guardan `inversionista_nombre` como texto, no el FK.
        participacion: dict[int, dict[str, float]] = {}
        for r in ProyectoInversionista.objects.filter(
            Q(fecha_fin__isnull=True) | Q(fecha_fin__gte=hoy)
        ).select_related("cliente"):
            if r.porcentaje_participacion is None:
                continue
            nombre = ((r.cliente.razon_social_nombre if r.cliente else "") or "").strip().upper()
            participacion.setdefault(r.proyecto_id, {})[nombre] = float(
                r.porcentaje_participacion
            )

        self.stdout.write(f"Plantas con contrato de representación vivo: {len(por_planta)}")
        varios = {p: cs for p, cs in por_planta.items() if len(cs) > 1}
        self.stdout.write(f"De esas, con MÁS DE UNO: {len(varios)}")

        afectadas, sin_participacion = [], []
        for pid, cs in sorted(varios.items()):
            tarifas = {
                (c.inversionista_nombre or "").strip().upper():
                    float(c.tarifa_representacion or 0)
                for c in cs
            }
            if len(set(tarifas.values())) <= 1:
                continue  # misma tarifa: da igual cuál se elija

            elegido = elegir_contrato_representacion(cs)
            t_hoy = float(elegido.tarifa_representacion or 0) if elegido else 0.0

            pesos = participacion.get(pid, {})
            if not pesos:
                sin_participacion.append(pid)
                continue

            total = sum(pesos.values()) or 1.0
            t_real = sum(tarifas.get(inv, 0.0) * (pct / total) for inv, pct in pesos.items())
            if abs(t_real - t_hoy) < 1e-9:
                continue

            desvio = (t_real - t_hoy) / t_hoy * 100 if t_hoy else float("inf")
            afectadas.append((pid, t_hoy, t_real, desvio, tarifas))

        self._imprimir(afectadas, sin_participacion, plantas)

    def _imprimir(self, afectadas, sin_participacion, plantas):
        self.stdout.write("")
        if not afectadas:
            self.stdout.write(self.style.SUCCESS(
                "Ninguna planta cambia de costo: donde hay varios contratos, "
                "las tarifas coinciden."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f"PLANTAS DONDE EL COSTO CAMBIARÍA: {len(afectadas)}\n"
            ))
            self.stdout.write(
                "  %-5s %-30s %10s %10s %9s" % ("id", "planta", "hoy", "real", "desvío")
            )
            self.stdout.write("  " + "-" * 70)
            for pid, t_hoy, t_real, desvio, tarifas in sorted(
                afectadas, key=lambda x: -abs(x[3])
            ):
                nombre = (plantas[pid].nombre_comercial or "")[:30]
                self.stdout.write(
                    "  %-5s %-30s %10.4f %10.4f %8.1f%%"
                    % (pid, nombre, t_hoy, t_real, desvio)
                )
                for inv, t in sorted(tarifas.items()):
                    self.stdout.write("        %-40s %10.4f" % (inv[:40] or "(sin nombre)", t))

        if sin_participacion:
            self.stdout.write(self.style.WARNING(
                f"\n{len(sin_participacion)} planta(s) con tarifas distintas y SIN "
                "participación vigente: no se puede ponderar, hay que revisarlas a mano."
            ))
            for pid in sin_participacion:
                self.stdout.write("  %-5s %s" % (pid, plantas[pid].nombre_comercial))
