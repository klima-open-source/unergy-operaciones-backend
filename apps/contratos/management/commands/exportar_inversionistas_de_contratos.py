"""Lista lo que los contratos de servicio decían de su planta e inversionista. SOLO LEE.

    python manage.py exportar_inversionistas_de_contratos
    python manage.py exportar_inversionistas_de_contratos --csv inversionistas_contratos.csv

Para qué. Desde el paso 4b la planta y el inversionista de un contrato se leen de
`contrato_proyectos` y de la participación. Las columnas viejas de `contratos`
(`proyecto_id`, `inversionista_id`, `inversionista_nombre`) siguen en la base hasta
`contratos/0023`, que las borra. La planta y el `inversionista_id` los vigila la
propia migración; lo que solo vive ahí es el NOMBRE escrito a mano. Este comando
saca, por contrato, lo escrito al lado de lo que dice hoy la participación:

- `sin participación`: el contrato nombraba un inversionista y hoy no tiene
  participación asignada (planta sin inversionistas cargados, o varios y no se
  sabe cuál). Es lo que hay que resolver a mano.
- `nombre distinto`: tiene participación, pero lo escrito no es igual a la razón
  social de la ficha (casi siempre otra grafía del mismo).
- `igual`: lo escrito coincide.

Se corre ANTES del despliegue de `0019`. Después las columnas ya no existen y el
comando lo dice en vez de fallar.
"""
import csv
import sys

from django.core.management.base import BaseCommand
from django.db import connection

CABECERA = [
    "contrato_id", "numero_contrato", "servicio", "proyecto_id", "proyecto",
    "inversionista_escrito", "inversionista_id_escrito", "inversionista_por_participacion",
    "estado",
]

CONSULTA = """
SELECT k.id, k.numero_contrato, k.servicio_aplica, cp.proyecto_id, p.nombre_comercial,
       trim(k.inversionista_nombre), k.inversionista_id, cl.razon_social_nombre,
       CASE
         WHEN cp.proyecto_inversionista_id IS NULL THEN 'sin participación'
         WHEN upper(trim(coalesce(k.inversionista_nombre, ''))) = upper(trim(cl.razon_social_nombre))
           THEN 'igual'
         ELSE 'nombre distinto'
       END
  FROM contratos k
  LEFT JOIN contrato_proyectos cp ON cp.contrato_id = k.id
  LEFT JOIN proyectos p ON p.id = cp.proyecto_id
  LEFT JOIN proyecto_inversionistas pi ON pi.id = cp.proyecto_inversionista_id
  LEFT JOIN inversionistas i ON i.id = pi.inversionista_id
  LEFT JOIN clientes cl ON cl.id = i.cliente_id
 WHERE k.grupo <> 'ppa'
   AND (coalesce(trim(k.inversionista_nombre), '') <> '' OR k.inversionista_id IS NOT NULL)
 ORDER BY 9 DESC, 5, 1
"""


def _columnas_existen(cursor) -> bool:
    cursor.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'contratos' AND column_name = 'inversionista_nombre'"
    )
    return cursor.fetchone()[0] == 1


class Command(BaseCommand):
    help = "Lista el inversionista escrito en cada contrato de servicio frente a su participación (solo lee)."

    def add_arguments(self, parser):
        parser.add_argument("--csv", dest="archivo", help="Escribe el resultado en este archivo CSV.")

    def handle(self, *args, archivo=None, **opciones):
        with connection.cursor() as cursor:
            if not _columnas_existen(cursor):
                self.stdout.write("Las columnas del inversionista ya no existen en `contratos`: nada que exportar.")
                return
            cursor.execute(CONSULTA)
            filas = cursor.fetchall()

        por_estado = {}
        for fila in filas:
            por_estado[fila[-1]] = por_estado.get(fila[-1], 0) + 1
        resumen = ", ".join(f"{e}: {n}" for e, n in sorted(por_estado.items())) or "ninguno"
        self.stdout.write(f"Contratos con inversionista escrito: {len(filas)} ({resumen}).")

        destino = open(archivo, "w", newline="", encoding="utf-8") if archivo else sys.stdout
        try:
            escritor = csv.writer(destino)
            escritor.writerow(CABECERA)
            escritor.writerows(filas)
        finally:
            if archivo:
                destino.close()
                self.stdout.write(f"Escrito en {archivo}.")
