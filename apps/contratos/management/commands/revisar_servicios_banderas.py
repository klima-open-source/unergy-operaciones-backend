"""Compara las banderas `srv_*` de una planta contra sus contratos reales.

**Solo lee. No escribe nada, y no tiene `--ejecutar`.**

    python manage.py revisar_servicios_banderas
    python manage.py revisar_servicios_banderas --csv salida.csv

Para qué. Hoy hay DOS respuestas a "¿qué servicios tiene esta planta?": las
banderas `srv_operacion`/`srv_representacion`/`srv_cgm`/`srv_ppa` de `Proyecto`,
que se ponen a mano en el formulario, y los contratos de `contratos_servicio` y
`ppa_contratos`. Nada las sincroniza. Este informe dice de qué tamaño es la
diferencia, planta por planta, antes de tocar nada.

Por qué importa que nadie las toque a ciegas: `srv_operacion` es el interruptor
del monitoreo. Si está apagada, el sondeo MGS no mira la planta, las alarmas de
desconexión no la vigilan y el informe FMO no la incluye. Derivarla del contrato
apagaría las plantas que operan sin contrato cargado --que las hay-- y eso no se
vería: la planta simplemente dejaría de reportar.

Las tres secciones responden lo que el plan necesita decidir
(`docs/SERVICIOS_AGRUPACION.md`, bloques C y D):

1. Las dos definiciones de "planta en operación" que hoy conviven y NO coinciden:
   `AND` en los paneles de O&M y arriendos y en el informe FMO, `OR` en
   portafolios. Lista las plantas donde dan distinto.
2. Bandera contra contrato, en los dos sentidos y para las cuatro banderas.
3. Las plantas que PERDERÍAN monitoreo si la bandera se derivara del contrato.
   Es la lista que hay que revisar a mano antes de migrar nada.

El contrato de CGM no se busca por `servicio_aplica='cgm'`: ese valor no se usa
--medido el 2026-09-17, 0 filas-- porque el CGM viaja en el mismo contrato de
representación, en `tarifa_cgm`. Se deriva con `grupos.subservicios_de()`, que
es la única definición de qué subservicios cubre un contrato.
"""
import csv

from django.core.management.base import BaseCommand

from apps.contratos.models import ContratoServicio
from apps.contratos.services import grupos
from apps.contratos.services import vigencia as vigencia_service
from apps.plataforma.services.fechas import hoy_col
from apps.ppa.models import PpaContrato, PpaContratoProyecto
from apps.proyectos.models import Proyecto

#: Bandera → qué subservicios la respaldarían, según los contratos.
RESPALDO = {
    "srv_operacion": {"mantenimiento", "arriendo", "internet"},
    "srv_representacion": {"representacion"},
    "srv_cgm": {"cgm"},
}


class Command(BaseCommand):
    help = "Compara las banderas srv_* contra los contratos vivos. Solo lee."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv", dest="csv_path", default=None,
            help="Escribe el detalle planta por planta en un CSV.",
        )

    def handle(self, *args, **opciones):
        plantas = {
            p.id: p for p in Proyecto.objects.filter(deleted_at__isnull=True)
        }
        subservicios = self._subservicios_por_planta(plantas)
        # Solo los PPA vivos, por la misma razón que los contratos de servicio.
        # `ppa_contratos` no tiene columna `estado`: su vigencia es de fechas.
        ppa_vivos = PpaContrato.objects.filter(
            vigencia_service.filtro_ppa_vivos(hoy_col()), deleted_at__isnull=True,
        )
        con_ppa = {
            pid for pid in PpaContratoProyecto.objects
            .filter(contrato__in=ppa_vivos)
            .values_list("proyecto_id", flat=True) if pid in plantas
        }

        self.stdout.write(f"Plantas vivas: {len(plantas)}\n")
        self._seccion_en_operacion(plantas)
        self._seccion_banderas(plantas, subservicios, con_ppa)
        self._seccion_riesgo(plantas, subservicios)

        if opciones["csv_path"]:
            self._csv(opciones["csv_path"], plantas, subservicios, con_ppa)
            self.stdout.write(
                self.style.SUCCESS(f"\nDetalle escrito en {opciones['csv_path']}")
            )

    @staticmethod
    def _subservicios_por_planta(plantas) -> dict[int, set[str]]:
        """Qué subservicios cubren los contratos VIVOS de cada planta.

        Solo los vivos: un contrato terminado o vencido no respalda una bandera.
        La definición es `vigencia.filtro_vivos`, la misma que usan el informe
        FMO y la alerta de aniversario -- incluida la mitad que falta en un
        filtro por `estado` a secas: el 2026-09-17 había 8 contratos de
        representación con la fecha fin pasada que decían `estado='vigente'`.
        """
        salida: dict[int, set[str]] = {}
        vivos = ContratoServicio.objects.filter(
            vigencia_service.filtro_vivos(hoy_col()), proyecto__isnull=False,
        )
        for contrato in vivos:
            if contrato.proyecto_id in plantas:
                salida.setdefault(contrato.proyecto_id, set()).update(
                    grupos.subservicios_de(contrato)
                )
        return salida

    def _titulo(self, texto):
        self.stdout.write("\n" + texto)
        self.stdout.write("-" * len(texto))

    def _fila(self, planta, extra=""):
        nombre = (planta.nombre_comercial or "")[:40]
        self.stdout.write(f"  {planta.id:<6} {nombre:<40} {planta.estado or '—':<15}{extra}")

    # ── 1. Las dos definiciones de "en operación" ─────────────────────────

    def _seccion_en_operacion(self, plantas):
        en_estado = {p.id for p in plantas.values() if p.estado == "en_operacion"}
        con_bandera = {p.id for p in plantas.values() if p.srv_operacion}
        y = en_estado & con_bandera
        o = en_estado | con_bandera

        self._titulo("1. 'Planta en operación': las dos definiciones que conviven")
        self.stdout.write(
            f"  AND  (paneles O&M y arriendos, informe FMO, reconectadores): {len(y)}"
        )
        self.stdout.write(f"  OR   (portafolios):                                         {len(o)}")
        self.stdout.write(f"  Difieren: {len(o - y)} plantas\n")

        solo_estado = sorted(en_estado - con_bandera)
        if solo_estado:
            self.stdout.write(
                f"  en_operacion pero srv_operacion=False ({len(solo_estado)}) "
                "— portafolios las cuenta, el informe FMO no:"
            )
            for pid in solo_estado:
                self._fila(plantas[pid])

        solo_bandera = sorted(con_bandera - en_estado)
        if solo_bandera:
            self.stdout.write(
                f"\n  srv_operacion=True pero el estado es otro ({len(solo_bandera)}) "
                "— se monitorean aunque no figuren en operación:"
            )
            for pid in solo_bandera:
                self._fila(plantas[pid])

    # ── 2. Bandera contra contrato ────────────────────────────────────────

    def _seccion_banderas(self, plantas, subservicios, con_ppa):
        self._titulo("2. Banderas srv_* contra los contratos")
        for campo, respaldo in RESPALDO.items():
            con_bandera = {p.id for p in plantas.values() if getattr(p, campo)}
            con_contrato = {
                pid for pid, subs in subservicios.items() if subs & respaldo
            }
            self._resumen(campo, con_bandera, con_contrato)

        con_bandera_ppa = {p.id for p in plantas.values() if p.srv_ppa}
        self._resumen("srv_ppa", con_bandera_ppa, con_ppa)

    def _resumen(self, campo, con_bandera, con_contrato):
        self.stdout.write(
            f"  {campo:<20} bandera={len(con_bandera):<4} contrato={len(con_contrato):<4} "
            f"| bandera sin contrato={len(con_bandera - con_contrato):<4} "
            f"| contrato sin bandera={len(con_contrato - con_bandera)}"
        )

    # ── 3. Lo que se apagaría ─────────────────────────────────────────────

    def _seccion_riesgo(self, plantas, subservicios):
        con_bandera = {p.id for p in plantas.values() if p.srv_operacion}
        con_contrato = {
            pid for pid, subs in subservicios.items()
            if subs & RESPALDO["srv_operacion"]
        }
        sin_respaldo = sorted(con_bandera - con_contrato)

        self._titulo("3. Plantas que PERDERÍAN monitoreo si la bandera se derivara")
        if not sin_respaldo:
            self.stdout.write(self.style.SUCCESS("  Ninguna. La derivación sería segura."))
            return sin_respaldo

        self.stdout.write(self.style.WARNING(
            f"  {len(sin_respaldo)} plantas con srv_operacion=True y SIN contrato de "
            "operación cargado.\n  Derivar la bandera las sacaría del sondeo MGS, de "
            "las alarmas de desconexión y del informe FMO."
        ))
        for pid in sin_respaldo:
            planta = plantas[pid]
            otros = sorted(subservicios.get(pid, set()))
            self._fila(planta, f" otros contratos: {', '.join(otros) or 'ninguno'}")
        return sin_respaldo

    # ── CSV ───────────────────────────────────────────────────────────────

    def _csv(self, ruta, plantas, subservicios, con_ppa):
        with open(ruta, "w", newline="", encoding="utf-8") as archivo:
            escritor = csv.writer(archivo)
            escritor.writerow([
                "proyecto_id", "nombre", "estado",
                "srv_operacion", "srv_representacion", "srv_cgm", "srv_ppa",
                "subservicios_por_contrato", "tiene_ppa",
                "operacion_sin_contrato", "en_operacion_sin_bandera",
            ])
            for pid, planta in sorted(plantas.items()):
                subs = subservicios.get(pid, set())
                escritor.writerow([
                    pid, planta.nombre_comercial, planta.estado,
                    planta.srv_operacion, planta.srv_representacion,
                    planta.srv_cgm, planta.srv_ppa,
                    "|".join(sorted(subs)), pid in con_ppa,
                    planta.srv_operacion and not (subs & RESPALDO["srv_operacion"]),
                    planta.estado == "en_operacion" and not planta.srv_operacion,
                ])
