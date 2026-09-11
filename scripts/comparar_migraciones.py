"""Compara las migraciones APLICADAS en la base contra las que trae el repo.

Responde una pregunta concreta: ¿alguien aplicó algo que no está en `master`?

**Solo lee.** No aplica, no revierte, no borra. Pensado para correrlo en el
servidor cuando haya dudas sobre quién tocó el esquema:

    docker compose exec operaciones python scripts/comparar_migraciones.py

Tres resultados posibles, y solo uno es motivo de alarma:

  * **Aplicadas sin archivo en el repo**: alguien corrió una migración desde su
    máquina con un código que no está en `master`. Es lo que hay que investigar.
  * **En el repo sin aplicar**: normal si el deploy todavía no ha corrido.
  * **Todo cuadra**: el esquema es exactamente el que describe `master`.

La salida es ASCII a propósito: la consola de Windows (cp1252) revienta con
emojis y este script tiene que poder correrse desde cualquier máquina.

La columna `applied` trae la hora de cada una: cruzarla contra el historial de
deploys dice si fue el pipeline o una persona.
"""
import os
import sys
from pathlib import Path

import django

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.db import connection  # noqa: E402
from django.db.migrations.loader import MigrationLoader  # noqa: E402


def main() -> int:
    with connection.cursor() as cur:
        cur.execute("SELECT current_database(), inet_server_addr()::text")
        base, servidor = cur.fetchone()
        cur.execute(
            "SELECT app, name, applied FROM django_migrations ORDER BY applied, id"
        )
        aplicadas = cur.fetchall()

    print(f"Base: {base}   servidor: {servidor}")
    print(f"Migraciones aplicadas: {len(aplicadas)}\n")

    # Lo que el repo declara, leído del disco (es decir: de `master`).
    en_repo = set(MigrationLoader(None, ignore_no_migrations=True).disk_migrations)

    huerfanas = [(a, n, f) for a, n, f in aplicadas if (a, n) not in en_repo]
    sin_aplicar = sorted(en_repo - {(a, n) for a, n, _ in aplicadas})

    if huerfanas:
        print("!! APLICADAS EN LA BASE PERO SIN ARCHIVO EN EL REPO")
        print("    Alguien las corrio con un codigo que no esta en master:\n")
        for app, nombre, cuando in huerfanas:
            print(f"      {cuando:%Y-%m-%d %H:%M}  {app}.{nombre}")
    else:
        print("OK: todas las migraciones aplicadas existen en el repo.")

    print()
    if sin_aplicar:
        print(f"Pendientes de aplicar ({len(sin_aplicar)}): normal si el deploy")
        print("no ha corrido todavia:\n")
        for app, nombre in sin_aplicar[:20]:
            print(f"      {app}.{nombre}")
        if len(sin_aplicar) > 20:
            print(f"      ... y {len(sin_aplicar) - 20} mas")
    else:
        print("Sin migraciones pendientes.")

    print("\n-- Las 15 mas recientes, con su hora --")
    for app, nombre, cuando in aplicadas[-15:]:
        marca = "  <-- SIN ARCHIVO" if (app, nombre) not in en_repo else ""
        print(f"   {cuando:%Y-%m-%d %H:%M}  {app}.{nombre}{marca}")

    return 1 if huerfanas else 0


if __name__ == "__main__":
    raise SystemExit(main())
