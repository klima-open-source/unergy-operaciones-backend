"""Copia el portafolio de cada planta a `portafolio_proyectos` (portafolios, paso 1).

**Por defecto solo lee.** Escribe únicamente con `--aplicar`.

    python manage.py copiar_portafolios
    python manage.py copiar_portafolios --aplicar

Para qué. Desde `proyectos/0013` una planta puede estar en varios portafolios, y
la pertenencia es una fila de `portafolio_proyectos`. Las asignaciones de hoy
viven en `proyectos.portafolio_id` (una por planta); este comando las copia, una
fila por planta con portafolio. Idempotente: lo que ya está no se repite.

Lee las columnas viejas con SQL directo: ya no son del modelo y siguen en la base
hasta `proyectos/0015` y `contratos/0026`. Después de esas migraciones dice que no
hay de dónde leer en vez de fallar.

También imprime, como respaldo en CSV, el texto suelto `contratos.portafolio`
(56 contratos de representación al 2026-10-15) antes de que `contratos/0026` lo
borre. Copiarlo del log del run de `comando.yml`.
"""
import csv

from django.core.management.base import BaseCommand
from django.db import connection, transaction

COPIAR = """
INSERT INTO portafolio_proyectos (portafolio_id, proyecto_id)
SELECT p.portafolio_id, p.id
  FROM proyectos p
 WHERE p.portafolio_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM portafolio_proyectos pp
                    WHERE pp.portafolio_id = p.portafolio_id AND pp.proyecto_id = p.id)
"""

RESPALDO_CONTRATOS = """
SELECT id, numero_contrato, grupo, portafolio
  FROM contratos
 WHERE portafolio IS NOT NULL AND trim(portafolio) <> ''
 ORDER BY id
"""


def _existe(tabla: str, columna: str) -> bool:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = %s",
            [tabla, columna],
        )
        return bool(cur.fetchone()[0])


class Command(BaseCommand):
    help = "Copia proyectos.portafolio_id a portafolio_proyectos (solo lee sin --aplicar)."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Escribe los cambios.")

    def handle(self, *args, aplicar=False, **opciones):
        if _existe("proyectos", "portafolio_id"):
            with transaction.atomic(), connection.cursor() as cur:
                cur.execute("SELECT count(*) FROM proyectos WHERE portafolio_id IS NOT NULL")
                con_portafolio = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM portafolio_proyectos")
                antes = cur.fetchone()[0]
                cur.execute(COPIAR)
                copiadas = cur.rowcount
                if not aplicar:
                    transaction.set_rollback(True)
            self.stdout.write(
                f"Plantas con portafolio: {con_portafolio}. Ya en portafolio_proyectos: "
                f"{antes}. Filas nuevas: {copiadas}."
            )
        else:
            self.stdout.write("proyectos.portafolio_id ya no existe (proyectos/0015): "
                              "no hay de dónde copiar.")

        if _existe("contratos", "portafolio"):
            with connection.cursor() as cur:
                cur.execute(RESPALDO_CONTRATOS)
                filas = cur.fetchall()
            self.stdout.write(f"\nRespaldo de contratos.portafolio ({len(filas)} contratos):")
            escritor = csv.writer(self.stdout, lineterminator="\n")
            escritor.writerow(["contrato_id", "numero_contrato", "grupo", "portafolio"])
            escritor.writerows(filas)

        if not aplicar:
            self.stdout.write("\nSolo lectura: corre con --aplicar para escribir.")
