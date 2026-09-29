"""Qué responde hoy cada API de sole.tech que usa la plataforma, y cuánto tarda.

**Solo lee. No escribe nada, ni en la base ni en sole.tech:** todas las
llamadas son GET.

Todo sole.tech entra con el mismo token (`Authorization: Token <TOKEN>`, ver
`apps/comun/sole_tech.py`), en sus tres hosts:

  - `data.sole.tech` (Solenium): alarmas, inversores de monitoreo y de puesta
    en marcha, potencia del dashboard, estado de los reconectadores.
  - `api.sole.tech` (SolarView): generación solar, flota, detalle.
  - `sunfactory.sole.tech`: el pipeline de obra.

Desde local no se puede correr: el `.env` local no trae el token. Va en el
servidor:

    docker compose exec operaciones python manage.py diagnosticar_sole_tech

`--flota` mide además lo que hace `generacion_hoy()`: una llamada a
`/generation/` por planta, de a 8 en paralelo, como en la petición web. Son unas
40 llamadas de lectura.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

import httpx
from django.core.management.base import BaseCommand

# Igual que los clientes: lo que se mide es lo que la plataforma espera hoy.
TIMEOUT_POR_DEFECTO = 30.0
HILOS_FLOTA = 8


@dataclass
class Medida:
    que: str
    url: str
    estado: int | None
    ms: int
    nota: str = ""

    @property
    def ok(self) -> bool:
        return self.estado is not None and 200 <= self.estado < 300


def _contar(cuerpo) -> str:
    """Cuántos elementos trajo, para ver que la respuesta tiene algo adentro."""
    if isinstance(cuerpo, list):
        return f"{len(cuerpo)} elementos"
    if isinstance(cuerpo, dict):
        datos = cuerpo.get("results", cuerpo)
        if isinstance(datos, list):
            return f"{len(datos)} elementos"
        if isinstance(datos, dict):
            return f"claves: {', '.join(list(datos)[:6])}"
    return ""


def medir(http: httpx.Client, que: str, metodo: str, url: str, **kwargs) -> tuple[Medida, object]:
    """Una llamada, medida. Nunca levanta: un timeout o un DNS caído son un
    resultado, no un error del comando."""
    inicio = time.monotonic()
    try:
        resp = http.request(metodo, url, **kwargs)
    except httpx.TimeoutException:
        return Medida(que, url, None, _ms(inicio), "timeout"), None
    except httpx.HTTPError as exc:
        return Medida(que, url, None, _ms(inicio), type(exc).__name__), None
    try:
        cuerpo = resp.json()
    except ValueError:
        cuerpo = None
    nota = _contar(cuerpo) if resp.is_success else resp.text[:120].replace("\n", " ")
    return Medida(que, url, resp.status_code, _ms(inicio), nota), cuerpo


def _ms(inicio: float) -> int:
    return int((time.monotonic() - inicio) * 1000)


def ids_enteros(valores) -> list[int]:
    """`project_id_*` son texto en la base, y alguno puede venir vacío o raro."""
    ids = []
    for v in valores:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    return ids


class Command(BaseCommand):
    help = "Mide qué responde cada API de sole.tech que usa la plataforma (solo lectura)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--flota", action="store_true",
            help="Mide también /generation/ para todas las plantas, como generacion_hoy().",
        )
        parser.add_argument(
            "--timeout", type=float, default=TIMEOUT_POR_DEFECTO,
            help=f"Segundos de espera por llamada (por defecto {TIMEOUT_POR_DEFECTO:g}).",
        )

    def handle(self, *args, **opciones):
        from apps.comun.config import settings
        from apps.plataforma.services.fechas import hoy_col
        from apps.proyectos.models import Proyecto

        hoy = hoy_col()
        ayer = hoy - timedelta(days=1)
        en_operacion = Proyecto.objects.filter(estado="en_operacion", deleted_at__isnull=True)
        ids_solenium = ids_enteros(en_operacion.values_list("project_id_solenium", flat=True))
        ids_solarview = ids_enteros(en_operacion.values_list("project_id_solarview", flat=True))
        muestra_solenium = ids_solenium[0] if ids_solenium else None
        muestra_solarview = ids_solarview[0] if ids_solarview else None

        medidas: list[Medida] = []
        with httpx.Client(timeout=opciones["timeout"], follow_redirects=True) as http:
            medidas += self._solenium(http, settings, muestra_solenium)
            medidas += self._solarview(http, settings, muestra_solarview, ayer, hoy)
            if opciones["flota"]:
                self._flota(http, settings, ids_solarview, ayer, hoy)

        self._imprimir(medidas)

    # ── data.sole.tech (Solenium) y sunfactory.sole.tech ────────────────────

    def _solenium(self, http, settings, sol_id) -> list[Medida]:
        from apps.comun import sole_tech

        self._titulo("data.sole.tech (Solenium) y sunfactory.sole.tech")
        if not sole_tech.configurado():
            self.stdout.write(self.style.WARNING("  SOLARVIEW_TOKEN vacío en este .env"))
            return []

        data = settings.SOLENIUM_DATA_URL.rstrip("/")
        sunfactory = settings.SUNFACTORY_API_URL.rstrip("/")
        llamadas = [
            ("proyectos (inversores de monitoreo)", f"{data}/project/", {"menu": 1}),
            ("disponibilidad (alarmas)", f"{data}/project_availability/", None),
            ("resumen de flota (dashboard)", f"{data}/project_summary/", None),
            ("proyectos de Sun Factory (pipeline de obra)", f"{sunfactory}/project/", {"limit": 1}),
        ]
        if sol_id:
            llamadas += [
                (f"inversores de {sol_id} (monitoreo / puesta en marcha)",
                 f"{data}/project/{sol_id}/inverter/", None),
                (f"relay de {sol_id} (reconectadores)", f"{data}/project/{sol_id}/relay/", None),
            ]
        return [
            medir(http, que, "GET", url, headers=sole_tech.cabeceras(), params=params)[0]
            for que, url, params in llamadas
        ]

    # ── Token: api.sole.tech ────────────────────────────────────────────────

    def _solarview(self, http, settings, sv_id, ayer, hoy) -> list[Medida]:
        from apps.comun import sole_tech

        self._titulo("api.sole.tech (SolarView)")
        if not sole_tech.configurado():
            self.stdout.write(self.style.WARNING("  SOLARVIEW_TOKEN vacío en este .env"))
            return []

        base = settings.SOLARVIEW_BASE_URL.rstrip("/")
        cabeceras = sole_tech.cabeceras()
        llamadas = [
            ("disponibilidad de la flota", f"{base}/solarview/kpis/availability/", None),
            ("proyectos de la compañía", f"{base}/solarview/config/company-projects/", None),
        ]
        if sv_id:
            llamadas += [
                (f"generación de {sv_id} (generacion_hoy)",
                 f"{base}/solarview/measurements/generation/",
                 {"project_id": sv_id, "start_date": ayer.isoformat(), "end_date": hoy.isoformat()}),
                (f"detalle de {sv_id} (medidor)",
                 f"{base}/solarview/config/project-detail/{sv_id}/", None),
                (f"inversores de {sv_id}",
                 f"{base}/solarview/measurements/inverters-list/", {"project_id": sv_id}),
                (f"histórico de reconectador de {sv_id}",
                 f"{base}/solarview/config/recloser/historical/",
                 {"recloser": sv_id, "start_date": f"{hoy.isoformat()} 00:00:00",
                  "end_date": f"{hoy.isoformat()} 23:59:59", "vars": "kw"}),
            ]
        return [
            medir(http, que, "GET", url, headers=cabeceras, params=params)[0]
            for que, url, params in llamadas
        ]

    # ── La carga de generacion_hoy(), medida ────────────────────────────────

    def _flota(self, http, settings, ids, ayer, hoy):
        self._titulo(f"generacion_hoy(): /generation/ por planta, de a {HILOS_FLOTA}")
        from apps.comun import sole_tech

        if not sole_tech.configurado():
            self.stdout.write(self.style.WARNING("  SOLARVIEW_TOKEN vacío: no se mide"))
            return
        url = f"{settings.SOLARVIEW_BASE_URL.rstrip('/')}/solarview/measurements/generation/"
        cabeceras = sole_tech.cabeceras()

        def una(sv_id: int) -> Medida:
            return medir(http, str(sv_id), "GET", url, headers=cabeceras, params={
                "project_id": sv_id, "start_date": ayer.isoformat(), "end_date": hoy.isoformat(),
            })[0]

        inicio = time.monotonic()
        with ThreadPoolExecutor(max_workers=HILOS_FLOTA) as pool:
            resultados = list(pool.map(una, ids))
        total = _ms(inicio)

        fallas = [m for m in resultados if not m.ok]
        lentas = sorted(resultados, key=lambda m: -m.ms)[:5]
        self.stdout.write(f"  {len(ids)} plantas · {total / 1000:.1f} s en total · {len(fallas)} fallaron")
        self.stdout.write("  Las 5 más lentas: " + ", ".join(f"{m.que} {m.ms} ms" for m in lentas))
        for m in fallas:
            self.stdout.write(self.style.ERROR(f"  falló {m.que}: {m.estado or '-'} {m.nota}"))
        if total > 120_000:
            self.stdout.write(self.style.ERROR(
                "  Más de 120 s: dentro de una petición web, gunicorn la habría cortado (502)."
            ))

    # ── Salida ──────────────────────────────────────────────────────────────

    def _titulo(self, texto: str):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(texto))

    def _imprimir(self, medidas: list[Medida]):
        self._titulo("Resultado")
        for m in medidas:
            estilo = self.style.SUCCESS if m.ok else self.style.ERROR
            marca = "OK   " if m.ok else "FALLA"
            self.stdout.write(estilo(
                f"  {marca} {m.estado or '-':>4} {m.ms:>6} ms  {m.que}  {m.nota}"
            ))
