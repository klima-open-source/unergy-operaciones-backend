"""Lista las coordenadas que no caen en Colombia. SOLO LEE.

    python manage.py revisar_coordenadas

Por qué. Hasta el 2026-10 las coordenadas se escribían en un campo numérico que
no avisaba nada: se colaron longitudes sin el signo menos (que caen en el océano
Índico) y pares con latitud y longitud invertidas. El front ahora avisa
(`CoordenadasInput`), pero lo ya guardado hay que revisarlo a mano.

Qué revisa. Proyectos (`latitud`, `longitud`) y contratos de servicio
(`ubicacion_lat`, `ubicacion_lng`, el servicio de Internet). Para cada fila fuera
de Colombia dice qué parece haber pasado y qué valor sugiere. No corrige nada:
la sugerencia se confirma contra la ubicación real de la planta.
"""
from django.core.management.base import BaseCommand

from apps.contratos.models import Contrato
from apps.plataforma.services.coordenadas import diagnosticar
from apps.proyectos.models import Proyecto


class Command(BaseCommand):
    help = "Lista las coordenadas de proyectos y contratos que no caen en Colombia (solo lee)."

    def handle(self, *args, **opciones):
        filas = []
        con_coordenadas = 0

        for p in Proyecto.objects.exclude(latitud=None, longitud=None).only("id", "nombre_comercial", "latitud", "longitud"):
            con_coordenadas += 1
            d = diagnosticar(p.latitud, p.longitud)
            if d:
                filas.append(("Proyecto", p.id, p.nombre_comercial or "", p.latitud, p.longitud, *d))

        contratos = (Contrato.objects.exclude(ubicacion_lat=None, ubicacion_lng=None)
                     .select_related("proyecto").only("id", "servicio_aplica", "ubicacion_lat", "ubicacion_lng",
                                                      "proyecto__nombre_comercial"))
        for c in contratos:
            con_coordenadas += 1
            d = diagnosticar(c.ubicacion_lat, c.ubicacion_lng)
            if d:
                planta = c.proyecto.nombre_comercial if c.proyecto_id else "sin proyecto"
                filas.append(("Contrato", c.id, f"{c.servicio_aplica} · {planta}", c.ubicacion_lat, c.ubicacion_lng, *d))

        self.stdout.write(f"Filas con coordenadas: {con_coordenadas}. Fuera de Colombia: {len(filas)}.\n")
        for tipo, id_, nombre, lat, lng, problema, sugerencia in filas:
            linea = f"{tipo} {id_} · {nombre} · ({lat}, {lng}) · {problema}"
            if sugerencia:
                linea += f" · sugerido: {sugerencia}"
            self.stdout.write(linea)
