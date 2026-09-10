"""Revisa la coherencia entre la potencia AC y la capacidad pico de cada planta.

**Solo reporta. No escribe nunca**, y no es por prudencia genérica: las
correcciones no se pueden adivinar. Una planta con AC 3.000 y pico 1.230 puede
tener mal la AC o mal la pico, y elegir es una decisión de operación, no de un
comando. Lo que esto hace es poner las filas raras en una lista corta para que
alguien las revise con el dato real a la mano.

    docker compose exec operaciones python manage.py revisar_potencias

Qué mira, y por qué:

  - **Pico menor que la AC.** Físicamente imposible: los paneles (DC) siempre
    suman más que el punto de conexión (AC). Alguna de las dos está mal.
  - **Relación fuera de banda.** En las plantas sanas la pico va de 1,0 a 1,6
    veces la AC; la banda se calcula con los datos de la propia base, no se
    asume. Una relación de 10 es un cero de más al teclear.
  - **Sin pico cargada.** No es un error, pero sin ese dato no se puede validar
    la AC ni calcular el sobredimensionado.

El origen de esto: al consolidar la potencia AC en una sola columna (migración
0005) se compararon las dos tablas y aparecieron 13 filas incoherentes en la
capacidad pico -- entre ellas tres con 10.000 kWp para plantas de 990 kW AC y
tres con 1 kWp.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.proyectos.models import Proyecto

# Fuera de esta banda, una relación pico/AC es sospechosa. Es amplia a
# propósito: acota errores de tecleo (un cero de más), no diferencias de diseño.
RATIO_MIN = Decimal("1.0")
RATIO_MAX = Decimal("2.0")


class Command(BaseCommand):
    help = "Reporta plantas cuya potencia AC y capacidad pico no son coherentes."

    def handle(self, *args, **opciones):
        filas = [
            (p.id, p.nombre_comercial, p.potencia_ac_kw, _pico(p))
            for p in Proyecto.objects.filter(deleted_at__isnull=True)
            .prefetch_related("info_tecnica")
            .order_by("nombre_comercial")
        ]
        con_ambas = [f for f in filas if f[2] and f[3]]

        imposibles = [f for f in con_ambas if f[3] < f[2]]
        fuera_de_banda = [
            f for f in con_ambas
            if f[3] >= f[2] and not (RATIO_MIN <= f[3] / f[2] <= RATIO_MAX)
        ]
        sin_pico = [f for f in filas if f[2] and not f[3]]
        sin_ac = [f for f in filas if not f[2]]

        sanas = [f for f in con_ambas if f not in imposibles and f not in fuera_de_banda]
        self.stdout.write(f"Proyectos vivos:        {len(filas)}")
        self.stdout.write(f"Con AC y pico:          {len(con_ambas)}")
        if sanas:
            ratios = sorted((f[3] / f[2]) for f in sanas)
            self.stdout.write(
                f"Relación pico/AC sana:  {ratios[0]:.2f} a {ratios[-1]:.2f} "
                f"({len(sanas)} plantas)"
            )

        self._listar(
            "PICO MENOR QUE LA AC -- imposible, una de las dos está mal",
            imposibles, self.style.ERROR,
        )
        self._listar(
            f"RELACIÓN FUERA DE {RATIO_MIN}-{RATIO_MAX} -- normalmente un cero de más",
            fuera_de_banda, self.style.WARNING,
        )
        self._listar("SIN CAPACIDAD PICO cargada", sin_pico, self.style.NOTICE)
        self._listar("SIN POTENCIA AC cargada", sin_ac, self.style.NOTICE)

        total = len(imposibles) + len(fuera_de_banda)
        if total:
            self.stdout.write(self.style.WARNING(
                f"\n{total} planta(s) con la potencia incoherente. Corregirlas es a "
                "mano (ficha del proyecto o Información técnica): cuál de los dos "
                "números está mal depende del diseño real de cada planta."
            ))
        else:
            self.stdout.write(self.style.SUCCESS("\nSin incoherencias."))

    def _listar(self, titulo, filas, estilo):
        if not filas:
            return
        self.stdout.write(estilo(f"\n{titulo} ({len(filas)}):"))
        for pid, nombre, ac, pico in filas:
            detalle = f"AC {ac}" if ac else "AC —"
            detalle += f" / pico {pico}" if pico else " / pico —"
            if ac and pico:
                detalle += f"  (x{pico / ac:.2f})"
            self.stdout.write(f"  #{pid} {nombre}: {detalle}")


def _pico(proyecto):
    """La capacidad pico (DC) vive en la info técnica, que puede no existir."""
    it = next(iter(proyecto.info_tecnica.all()), None)
    return it.capacidad_instalada_kwp if it else None
