"""Lista las partes de los contratos que solo tienen el nombre escrito. SOLO LEE.

    python manage.py exportar_partes_sin_cliente
    python manage.py exportar_partes_sin_cliente --csv partes_sin_cliente.csv

Para qué. Desde el 2026-10-07 las partes de un contrato viven en `contrato_partes`,
siempre vinculadas a un cliente, y el nombre y el NIT se leen de la ficha. Las que
nunca se vincularon --un nombre escrito a mano, sin cliente-- quedaron solo en las
12 columnas viejas de `contratos` (`comprador_*`, `vendedor_*`, `contratante_*`,
`prestador_*`), que la pantalla ya no muestra. La migración
`contratos/0014_borrar_columnas_partes` borra esas columnas: este comando las saca
ANTES, para la revisión de clientes (vincular cada una al cliente que corresponda).

Por eso se corre en producción antes del despliegue de limpieza. Después de ese
despliegue las columnas ya no existen y el comando lo dice en vez de fallar.

Lee las columnas con SQL porque ya no están en el modelo (`0013_partes_fuera_del_estado`).
"""
import csv
import sys

from django.core.management.base import BaseCommand
from django.db import connection

ROLES = ("comprador", "vendedor", "contratante", "prestador")
CABECERA = ["contrato_id", "grupo", "numero_contrato", "nombre_interno", "rol", "nombre", "nit"]


def _columnas_existen(cursor):
    cursor.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'contratos' AND column_name = ANY(%s)",
        [[f"{rol}_nombre" for rol in ROLES]],
    )
    return cursor.fetchone()[0] == len(ROLES)


def partes_sin_cliente(cursor):
    """Filas (contrato, rol, nombre, nit) con nombre escrito y sin cliente vinculado."""
    consultas = [
        f"""SELECT id, grupo, numero_contrato, nombre_interno, '{rol}', trim({rol}_nombre), {rol}_nit
              FROM contratos
             WHERE {rol}_id IS NULL AND coalesce(trim({rol}_nombre), '') <> ''"""
        for rol in ROLES
    ]
    cursor.execute(" UNION ALL ".join(consultas) + " ORDER BY 5, 6, 1")
    return cursor.fetchall()


class Command(BaseCommand):
    help = "Lista las partes de contratos que solo tienen el nombre escrito, sin cliente (solo lee)."

    def add_arguments(self, parser):
        parser.add_argument("--csv", dest="archivo", help="Escribe el resultado en este archivo CSV.")

    def handle(self, *args, archivo=None, **opciones):
        with connection.cursor() as cursor:
            if not _columnas_existen(cursor):
                self.stdout.write("Las columnas de partes ya no existen en `contratos`: no hay nada que exportar.")
                return
            filas = partes_sin_cliente(cursor)

        por_rol = {}
        for fila in filas:
            por_rol[fila[4]] = por_rol.get(fila[4], 0) + 1
        resumen = ", ".join(f"{rol}: {n}" for rol, n in sorted(por_rol.items())) or "ninguna"
        self.stdout.write(f"Partes con solo el nombre, sin cliente: {len(filas)} ({resumen}).")

        destino = open(archivo, "w", newline="", encoding="utf-8") if archivo else sys.stdout
        try:
            escritor = csv.writer(destino)
            escritor.writerow(CABECERA)
            escritor.writerows(filas)
        finally:
            if archivo:
                destino.close()
                self.stdout.write(f"Escrito en {archivo}.")
