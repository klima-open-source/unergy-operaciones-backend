"""Une el catálogo de operadores de red con el de originabot (TSF).

Para cada operador de originabot (`code` + nombre) llama a
`operadores_originabot.vincular()`: el que ya tiene el code se deja, el que
coincide por nombre se vincula y el que falta se crea. Idempotente: se puede
correr cuantas veces haga falta.

originabot no publica un listado de operadores: el catálogo completo viene en
`grid_operator_data.options` del detalle de cualquier proyecto, y de ahí se lee.

Va con el usuario de Operaciones en originabot (ORIGINABOT_USER y
ORIGINABOT_PASSWORD, ver `apps/comun/integraciones/originabot.py`). Uso:

    uv run python manage.py vincular_operadores_originabot --dry-run
    uv run python manage.py vincular_operadores_originabot

Si va a CREAR operadores, los lista y pide confirmación antes de escribir nada
(`--si` la salta, para correrlo sin terminal interactiva).
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.comun.integraciones.originabot import ClienteOriginabot, OriginabotError
from apps.fronteras.services import operadores_originabot


class _Deshacer(Exception):
    pass


class Command(BaseCommand):
    help = "Vincula (o crea) los operadores de red de originabot por su code."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Hace todo y lo deshace al final.")
        parser.add_argument("--si", action="store_true", help="No pedir confirmación para crear operadores.")

    def _catalogo(self) -> list[dict]:
        ob = ClienteOriginabot()
        try:
            resultados = ob.proyectos(page_size=1)
            if not resultados:
                raise CommandError("originabot no devolvió ningún proyecto del que leer el catálogo.")
            detalle = ob.proyecto(resultados[0]["id"])
        except OriginabotError as exc:
            raise CommandError(str(exc))
        opciones = (detalle.get("grid_operator_data") or {}).get("options") or []
        if not opciones:
            raise CommandError("El detalle del proyecto no trae grid_operator_data.options.")
        return opciones

    def handle(self, *args, **opts):
        opciones = self._catalogo()
        self.stdout.write(f"originabot ({settings.ORIGINABOT_URL}): {len(opciones)} operadores.")

        a_crear = []
        for o in opciones:
            try:
                _, estado = operadores_originabot.que_pasaria(o.get("code", ""), o.get("name", ""))
            except (ValueError, operadores_originabot.VinculoImposible):
                continue
            if estado == operadores_originabot.CREADO:
                a_crear.append(f"{o['code']} ({o['name']})")
        if a_crear and not opts["dry_run"] and not opts["si"]:
            self.stdout.write(f"Esta acción creará {len(a_crear)} operadores de red en Operaciones:")
            for linea in a_crear:
                self.stdout.write(f"  + {linea}")
            try:
                respuesta = input("¿Desea continuar? [s/N] ")
            except EOFError:
                raise CommandError("Sin terminal para confirmar: vuelve a correrlo con --si.")
            if respuesta.strip().lower() not in ("s", "si", "sí", "y", "yes"):
                self.stdout.write(self.style.WARNING("Cancelado: no se escribió nada."))
                return

        conteo = {operadores_originabot.EXISTENTE: 0, operadores_originabot.VINCULADO: 0, operadores_originabot.CREADO: 0}
        problemas = []
        try:
            with transaction.atomic():
                for o in opciones:
                    try:
                        operador, estado = operadores_originabot.vincular(o.get("code", ""), o.get("name", ""))
                    except (ValueError, operadores_originabot.VinculoImposible) as exc:
                        problemas.append(f"{o}: {exc}")
                        continue
                    conteo[estado] += 1
                    if estado != operadores_originabot.EXISTENTE:
                        self.stdout.write(f"  {estado:<9} {o['code']:<14} → #{operador.id} {operador.nombre_comercial}")
                if opts["dry_run"]:
                    raise _Deshacer
        except _Deshacer:
            self.stdout.write(self.style.WARNING("--dry-run: todo se deshizo."))

        self.stdout.write(self.style.SUCCESS(
            f"Existentes {conteo['existente']}, vinculados {conteo['vinculado']}, creados {conteo['creado']}."
        ))
        for p in problemas:
            self.stdout.write(self.style.ERROR(f"  sin vincular: {p}"))
