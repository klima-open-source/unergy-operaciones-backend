"""Siembra las categorías y tipos del reporte estructurado en el catálogo.

`aplicar_clasificacion` le pone a cada falla el `FallaCatTipo` de código
`"{categoria}.{subtipo}"`, para que las vistas que muestran `tipo.etiqueta` sigan
funcionando. Ese catálogo lo sembraba el arranque de FastAPI, que ya no corre: una
categoría nueva en `ESTRUCTURA_FALLAS` (p.ej. `equipos`) queda con `tipo=None`
hasta correr esto. Idempotente: crea lo que falta y actualiza etiquetas; no borra
nada ni toca el catálogo legacy numérico.

    python manage.py sembrar_tipos_fallas --dry-run
    python manage.py sembrar_tipos_fallas
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.monitoreo.models import FallaCatCategoria, FallaCatTipo
from apps.monitoreo.services.fallas.estructura import ESTRUCTURA_FALLAS, tipo_codigo

# Detrás del catálogo legacy, igual que el sembrado de FastAPI.
_ORDEN_BASE = 100


class Command(BaseCommand):
    help = "Siembra (idempotente) las categorías y tipos de ESTRUCTURA_FALLAS."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Solo reporta.")

    def handle(self, *args, dry_run: bool = False, **opts):
        creados: list[str] = []
        with transaction.atomic():
            for idx, cat in enumerate(ESTRUCTURA_FALLAS):
                categoria, nueva = FallaCatCategoria.objects.update_or_create(
                    codigo=cat["codigo"],
                    defaults={
                        "etiqueta": cat["etiqueta"], "icono": cat.get("icono"),
                        "color_hex": cat.get("color_hex"), "orden": _ORDEN_BASE + idx,
                        "activa": True,
                    },
                )
                if nueva:
                    creados.append(f"categoría {cat['codigo']}")
                for sub in [*cat.get("opciones", []), *cat.get("tipos_falla", [])]:
                    codigo = tipo_codigo(cat["codigo"], sub["codigo"])
                    _, nuevo = FallaCatTipo.objects.update_or_create(
                        codigo=codigo,
                        defaults={
                            "categoria": categoria, "etiqueta": sub["etiqueta"],
                            "activa": True,
                        },
                    )
                    if nuevo:
                        creados.append(f"tipo {codigo}")
            if dry_run:
                transaction.set_rollback(True)

        prefijo = "[dry-run] " if dry_run else ""
        self.stdout.write(f"{prefijo}{len(creados)} nuevos: {', '.join(creados) or '—'}")
