"""Respaldo en CSV de las columnas de `proyectos` que se van a borrar. **Solo lee.**

    python manage.py respaldar_columnas_proyectos

Imprime, solo de las plantas que tienen algún valor, las columnas que
`proyectos/0020` sacó del modelo: las banderas `srv_*`, `potencia_con_cen_mw`,
`codigo_cnd`, `fecha_fin_representacion` y `fecha_comercializacion_editada_manual`.
Se corre antes del despliegue que las borra y su salida se guarda fuera del repo.

Imprime nombres de planta y valores de esas columnas, nada sensible: el repo y
sus logs de Actions son públicos.
"""
import csv

from django.core.management.base import BaseCommand
from django.db import connection

COLUMNAS = [
    "srv_operacion", "srv_representacion", "srv_cgm", "srv_ppa", "potencia_con_cen_mw",
    "codigo_cnd", "fecha_fin_representacion", "fecha_comercializacion_editada_manual",
]


class Command(BaseCommand):
    help = "Imprime en CSV las columnas de proyectos que se van a borrar (solo lee)."

    def handle(self, *args, **opciones):
        with connection.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'proyectos' AND column_name = ANY(%s)", [COLUMNAS]
            )
            en_base = {r[0] for r in cur.fetchall()}
            existen = [c for c in COLUMNAS if c in en_base]
            if not existen:
                self.stdout.write("Ninguna de las columnas existe ya: no hay nada que respaldar.")
                return
            con_valor = " OR ".join(
                f"{c} IS TRUE" if c.startswith(("srv_", "fecha_comercializacion"))
                else f"{c} IS NOT NULL" for c in existen
            )
            cur.execute(
                f"SELECT id, nombre_comercial, {', '.join(existen)} FROM proyectos "
                f"WHERE {con_valor} ORDER BY id"
            )
            filas = cur.fetchall()

        self.stdout.write(f"Respaldo de {len(existen)} columnas, {len(filas)} plantas con valor:")
        escritor = csv.writer(self.stdout, lineterminator="\n")
        escritor.writerow(["id", "nombre_comercial", *existen])
        escritor.writerows(filas)
