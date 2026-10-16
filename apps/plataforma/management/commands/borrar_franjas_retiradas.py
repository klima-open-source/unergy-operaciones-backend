"""Borra del programador las franjas que ya no están en `config/horarios.py`.

    python manage.py borrar_franjas_retiradas            # solo lista
    python manage.py borrar_franjas_retiradas --aplicar  # las borra

Por qué. `django_celery_beat` guarda el horario en `django_celery_beat_periodictask`
y al arrancar copia ahí `HORARIOS`, pero nunca borra: una franja que se quita
del diccionario sigue en la tabla y `beat` la sigue disparando. El `worker` la
rechaza como tarea no registrada, todos los días. Primer caso: `evo-forecast-ingest`
(06:00), retirada el 2026-10-16 con el clima.

Qué toca. Solo las filas cuya tarea empieza por un dominio de `apps/` y cuyo
nombre no está en `HORARIOS`; las internas de Celery (`celery.backend_cleanup`)
no se tocan.
"""
from django.core.management.base import BaseCommand
from django_celery_beat.models import PeriodicTask

from config.horarios import HORARIOS


class Command(BaseCommand):
    help = "Lista (o borra con --aplicar) las franjas de beat que ya no están en config/horarios.py."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Borrarlas; sin esto solo lista.")

    def handle(self, *args, **opciones):
        retiradas = (
            PeriodicTask.objects.exclude(name__in=HORARIOS.keys())
            .exclude(task__startswith="celery.")
            .order_by("name")
        )
        if not retiradas:
            self.stdout.write("No hay franjas retiradas: el programador coincide con HORARIOS.")
            return
        for t in retiradas:
            self.stdout.write(f"  {t.name}  ->  {t.task}  (habilitada={t.enabled})")
        if not opciones["aplicar"]:
            self.stdout.write(f"{len(retiradas)} franja(s) retirada(s). Corre con --aplicar para borrarlas.")
            return
        borradas, _ = retiradas.delete()
        self.stdout.write(self.style.SUCCESS(f"Borradas {borradas} fila(s)."))
