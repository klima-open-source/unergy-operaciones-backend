"""Borra las dos tablas huérfanas de REC, si están vacías.

REC se retiró como línea de negocio (decisión D-4, `docs/DOMINIO_COMERCIAL.md`).
Las columnas y las banderas salieron por migración de Django; estas dos tablas
no, porque **ningún modelo las declara**: Django no las conoce y por eso
`makemigrations` no las puede borrar.

Va como management command y NO dentro de una migración a propósito
(`CLAUDE.md`): una migración que falle deja el deploy a medias y su error se
pierde. Acá el fallo se ve en la terminal de quien lo corre.

    # cuenta y no toca nada
    docker compose exec operaciones python manage.py limpiar_rec

    # borra, sólo las que estén vacías
    docker compose exec operaciones python manage.py limpiar_rec --confirmar

**Es de una sola vez: borrar este archivo después de correrlo.**

Seguro en los tres casos posibles:

  - la tabla no existe  → no hace nada (`DROP TABLE IF EXISTS` es un no-op)
  - existe y está vacía → la borra
  - existe y TIENE filas → **no la toca** y avisa. Esa es la única situación en
    la que se perdería un dato irrecuperable, y la decisión no es de un script.
"""

from django.core.management.base import BaseCommand
from django.db import connection, transaction

# El hijo primero: `rec_certificados` probablemente referencia a `rec_procesos`,
# y borrar el padre antes fallaría por la clave foránea. Sin CASCADE — arrastrar
# objetos dependientes que nadie enumeró es justo lo que no se quiere acá.
TABLAS = ("rec_certificados", "rec_procesos")


def _existe(cursor, tabla: str) -> bool:
    cursor.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = %s)",
        [tabla],
    )
    return bool(cursor.fetchone()[0])


def _filas(cursor, tabla: str) -> int:
    # El nombre viene de TABLAS, una constante del módulo: no hay entrada de
    # usuario que interpolar acá.
    cursor.execute(f'SELECT count(*) FROM "{tabla}"')
    return int(cursor.fetchone()[0])


class Command(BaseCommand):
    help = "Cuenta (y con --confirmar borra) las tablas huérfanas de REC."

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirmar", action="store_true",
            help="Borra las tablas que existan y estén vacías. Sin esto sólo cuenta.",
        )

    def handle(self, *args, **opciones):
        confirmar = opciones["confirmar"]
        estado: dict[str, int | None] = {}

        with connection.cursor() as cursor:
            for tabla in TABLAS:
                estado[tabla] = _filas(cursor, tabla) if _existe(cursor, tabla) else None

        for tabla, filas in estado.items():
            if filas is None:
                self.stdout.write(f"  {tabla}: no existe")
            elif filas == 0:
                self.stdout.write(f"  {tabla}: existe, vacía")
            else:
                self.stdout.write(self.style.WARNING(
                    f"  {tabla}: existe, {filas} fila(s)"
                ))

        con_datos = [t for t, f in estado.items() if f]
        if con_datos:
            self.stdout.write(self.style.ERROR(
                "\nNO se borra nada: " + ", ".join(con_datos) + " tiene(n) datos. "
                "Decidí primero si ese dato se archiva."
            ))
            return

        borrables = [t for t, f in estado.items() if f == 0]
        if not borrables:
            self.stdout.write(self.style.SUCCESS(
                "\nNada que hacer: ninguna de las dos tablas existe."
            ))
            return

        if not confirmar:
            self.stdout.write(
                f"\n{len(borrables)} tabla(s) vacía(s) listas para borrar. "
                "Repetí con --confirmar."
            )
            return

        with transaction.atomic(), connection.cursor() as cursor:
            for tabla in borrables:
                cursor.execute(f'DROP TABLE IF EXISTS "{tabla}"')
                self.stdout.write(self.style.SUCCESS(f"  {tabla}: borrada"))

        self.stdout.write(self.style.SUCCESS(
            "\nListo. Ahora borrá este comando: "
            "apps/contratos/management/commands/limpiar_rec.py"
        ))
