"""Une el catálogo de operadores de red con el de originabot (TSF).

Para cada operador de originabot (`code` + nombre) llama a
`operadores_originabot.vincular()`: el que ya tiene el code se deja, el que
coincide por nombre se vincula y el que falta se crea. Idempotente: se puede
correr cuantas veces haga falta.

originabot no publica un listado de operadores: el catálogo completo viene en
`grid_operator_data.options` del detalle de cualquier proyecto, y de ahí se lee.

Necesita un token de Unergy Auth del mismo entorno que ORIGINABOT_URL (el
backend no tiene credenciales propias para originabot). Uso:

    uv run python manage.py vincular_operadores_originabot --token "$TOKEN" --dry-run
    uv run python manage.py vincular_operadores_originabot --token "$TOKEN"

Si va a CREAR operadores, los lista y pide confirmación antes de escribir nada
(`--si` la salta, para correrlo sin terminal interactiva).

El token también se puede pasar en ORIGINABOT_TOKEN, para que no quede en el
historial de la shell.
"""
import os

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.fronteras.services import operadores_originabot


class _Deshacer(Exception):
    pass


class Command(BaseCommand):
    help = "Vincula (o crea) los operadores de red de originabot por su code."

    def add_arguments(self, parser):
        parser.add_argument("--token", help="Bearer de Unergy Auth. Por defecto, ORIGINABOT_TOKEN.")
        parser.add_argument("--dry-run", action="store_true", help="Hace todo y lo deshace al final.")
        parser.add_argument("--si", action="store_true", help="No pedir confirmación para crear operadores.")

    def _catalogo(self, token: str) -> list[dict]:
        base = settings.ORIGINABOT_URL.rstrip("/")
        with httpx.Client(timeout=60, headers={"Authorization": f"Bearer {token}"}) as cliente:
            lista = cliente.get(f"{base}/api/minifarm/projects/", params={"page_size": 1})
            if lista.status_code in (401, 403):
                raise CommandError(f"originabot rechazó el token ({lista.status_code}). ¿Es del mismo entorno que {base}?")
            lista.raise_for_status()
            resultados = lista.json().get("results") or []
            if not resultados:
                raise CommandError("originabot no devolvió ningún proyecto del que leer el catálogo.")
            detalle = cliente.get(f"{base}/api/minifarm/projects/{resultados[0]['id']}/")
            detalle.raise_for_status()
        opciones = (detalle.json().get("grid_operator_data") or {}).get("options") or []
        if not opciones:
            raise CommandError("El detalle del proyecto no trae grid_operator_data.options.")
        return opciones

    def handle(self, *args, **opts):
        token = opts.get("token") or os.getenv("ORIGINABOT_TOKEN")
        if not token:
            raise CommandError("Falta el token: --token o ORIGINABOT_TOKEN.")

        opciones = self._catalogo(token)
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
