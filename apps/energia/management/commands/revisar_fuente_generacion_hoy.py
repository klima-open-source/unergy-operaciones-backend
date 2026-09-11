"""Cuántas plantas están mostrando el kWh del MEDIDOR en vez del de inversores.

**Solo reporta. No escribe nada, y no toca la base.** Son dos llamadas a los
servicios que ya existen (las dos cacheadas), leídas y contadas.

Para qué. `generacion_hoy()` tiene un apaño: si los inversores de una planta no
devuelven datos, en vez de decirlo pone el kWh del medidor de frontera y marca
`fuente="medidor"`. Son dos mediciones de puntos distintos del circuito --el
medidor está aguas abajo del cableado y el trafo, y en una planta con
autoconsumo solo ve el excedente que sale a la red-- así que no son el mismo
número ni deberían ocupar el mismo lugar.

El problema no es la imprecisión, es que **tapa el hueco**. Una planta con la
telemetría caída aparece con un número creíble y un porcentaje de cumplimiento
razonable, y sale del radar justo en Fallas -> Monitoreo, que es la vista cuyo
trabajo es avisar de las plantas con problemas.

Antes de quitar el apaño hay que saber a cuántas plantas les toca hoy, porque
las dos respuestas llevan a decisiones distintas:

  - **Pocas**: quitarlo es trivial, esas pasan a mostrar "S/D".
  - **Muchas**: hay una falla de telemetría de fondo que el apaño lleva tiempo
    tapando, y ESE es el hallazgo. Quitarlo primero solo cambiaría un número
    engañoso por un tablero lleno de huecos.

Se corre en el servidor, que es donde están las credenciales de SolarView:

    docker compose exec operaciones python manage.py revisar_fuente_generacion_hoy

`--detalle` lista planta por planta en vez del resumen.
"""
from django.core.management.base import BaseCommand

ETIQUETAS = {
    "inversor": "INV  inversores",
    "medidor": "MED  medidor de frontera (el apaño)",
    "sin_dato": "S/D  ninguna de las dos",
}


class Command(BaseCommand):
    help = "Reporta de qué fuente sale el kWh de hoy de cada planta."

    def add_arguments(self, parser):
        parser.add_argument(
            "--detalle", action="store_true",
            help="Lista planta por planta, no solo el resumen.",
        )

    def handle(self, *args, **opciones):
        from apps.energia.services import solarview_monitoreo as sv

        # `proyectos` acá, `projects` en monitoreo_flota: los dos endpoints no
        # usan el mismo idioma para la misma cosa.
        filas = (sv.generacion_hoy() or {}).get("proyectos") or []
        if not filas:
            self.stdout.write(self.style.WARNING(
                "Sin filas. O SolarView no respondió, o ningún proyecto en "
                "operación tiene su project_id_solarview reconciliado."
            ))
            return

        # El estado de la flota dice si la planta está REALMENTE caída o si solo
        # se cayó su telemetría: son dos problemas distintos y se arreglan en
        # lugares distintos.
        estados = {}
        try:
            flota = sv.monitoreo_flota() or {}
            estados = {f["proyecto_id"]: f.get("status") for f in flota.get("projects", [])}
        except Exception as exc:  # noqa: BLE001 -- el reporte sirve igual sin esto
            self.stdout.write(self.style.WARNING(f"Sin estado de flota: {exc}\n"))

        por_fuente = {"inversor": [], "medidor": [], "sin_dato": []}
        for f in filas:
            por_fuente.setdefault(f.get("fuente") or "sin_dato", []).append(f)

        total = len(filas)
        self.stdout.write(f"Plantas en operación con id de SolarView: {total}\n")
        for clave in ("inversor", "medidor", "sin_dato"):
            cuantas = len(por_fuente.get(clave, []))
            pct = round(cuantas / total * 100) if total else 0
            estilo = self.style.ERROR if clave == "medidor" and cuantas else str
            self.stdout.write(estilo(f"  {ETIQUETAS[clave]:<40} {cuantas:>3}  ({pct}%)"))

        medidor = por_fuente.get("medidor", [])
        if not medidor:
            self.stdout.write(self.style.SUCCESS(
                "\nNinguna planta está usando el medidor en lugar de los "
                "inversores. Quitar el apaño no cambia nada de lo que se ve hoy."
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f"\n{len(medidor)} planta(s) muestran hoy el kWh del medidor. "
                "Al quitar el apaño pasan a mostrar 'S/D' y el número de abajo "
                "desaparece (el medidor sigue en su propio recuadro de la "
                "tarjeta, con su hora de corte)."
            ))
            for f in sorted(medidor, key=lambda x: x["nombre"] or ""):
                estado = estados.get(f["proyecto_id"]) or "?"
                self.stdout.write(
                    f"  #{f['proyecto_id']} {f['nombre']}: "
                    f"{f['kwh_real']} kWh  [flota: {estado}]"
                )
            self.stdout.write(
                "\n  'flota: caido' o 'sin_comunicacion' explica el hueco: la planta o su\n"
                "  enlace están abajo. 'online' o 'degradado' NO lo explica -- ahí la\n"
                "  planta reporta disponibilidad pero no entrega generación, y eso es un\n"
                "  problema de telemetría que el apaño viene tapando."
            )

        if opciones["detalle"]:
            self.stdout.write(self.style.NOTICE("\nPlanta por planta:"))
            for f in sorted(filas, key=lambda x: x["nombre"] or ""):
                etiqueta = (f.get("fuente") or "sin_dato")[:3].upper()
                estado = estados.get(f["proyecto_id"]) or "?"
                self.stdout.write(
                    f"  [{etiqueta}] #{f['proyecto_id']} {f['nombre']}: "
                    f"{f['kwh_real']} kWh  [flota: {estado}]"
                )
