"""Guarda en cada planta su minigranja de originabot (`originabot_minifarm_id`).

Es la llave con TSF: con ella la unión de PPAs y la sugerencia de proyectos que
faltan (modal «Nuevo proyecto») cruzan por id y no por nombre. Solo toca plantas
que aún no la tienen y que traen algún código (`origina_code` o `codigo_tsf`);
la regla para resolverla está en `apps/proyectos/services/originabot_minigranjas.py`.

No crea nada en ningún sistema: solo escribe el vínculo aquí. Idempotente.

    uv run python manage.py vincular_minigranjas_originabot --dry-run
    uv run python manage.py vincular_minigranjas_originabot

Va con el usuario de Operaciones en originabot (ORIGINABOT_USER y
ORIGINABOT_PASSWORD, ver `apps/comun/integraciones/originabot.py`).
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.comun.integraciones.originabot import ClienteOriginabot, OriginabotError
from apps.proyectos.models import Proyecto
from apps.proyectos.services import originabot_minigranjas as minigranjas


class Command(BaseCommand):
    help = "Guarda en cada planta su minigranja de originabot."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Solo informa; no guarda nada.")

    def handle(self, *args, **opts):
        ob = ClienteOriginabot()

        pendientes = (
            Proyecto.objects.filter(deleted_at__isnull=True, originabot_minifarm_id__isnull=True)
            .filter(Q(origina_code__isnull=False) | Q(codigo_tsf__isnull=False))
            .order_by("id")
        )
        self.stdout.write(f"originabot ({settings.ORIGINABOT_URL}): {pendientes.count()} plantas por vincular.")

        vinculadas, sin_resolver = 0, []
        try:
            for proyecto in pendientes:
                if opts["dry_run"]:
                    minifarm = minigranjas.resolver(proyecto, ob)
                else:
                    minifarm = minigranjas.minigranja_de(proyecto, ob)
                if minifarm:
                    vinculadas += 1
                else:
                    sin_resolver.append(f"#{proyecto.id} {proyecto.nombre_comercial}")
        except OriginabotError as exc:
            raise CommandError(str(exc))

        verbo = "se vincularían" if opts["dry_run"] else "vinculadas"
        self.stdout.write(self.style.SUCCESS(f"{vinculadas} {verbo}; {len(sin_resolver)} sin minigranja única."))
        for linea in sin_resolver:
            self.stdout.write(f"  sin resolver: {linea}")
