"""Qué responde hoy cada API de sole.tech que usa la plataforma, y cuánto tarda.

**Solo lee. No escribe nada, ni en la base ni en sole.tech:** todas las
llamadas son GET.

Prueba el token (`Authorization: Token <TOKEN>`, ver `apps/comun/sole_tech.py`)
en los tres hosts de sole.tech que usa la plataforma:

  - `api.sole.tech` (SolarView): generación solar, flota, detalle, inversores,
    alarmas y reconectadores. Es el único que lo acepta (medido el 2026-09-29).
  - `data.sole.tech` (Solenium, la API vieja) y `sunfactory.sole.tech`: lo
    rechazan. Se prueban igual, para ver el día que eso cambie.

En el servidor:

    docker compose exec operaciones python manage.py diagnosticar_sole_tech

En local hace falta el token en el `.env`. Si la base no está a mano,
`--sin-base` toma como muestra el primer proyecto del listado de SolarView:

    python manage.py diagnosticar_sole_tech --sin-base

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


def primer_id(cuerpo) -> int | None:
    """El id del primer proyecto de un listado de sole.tech, venga como lista o
    como `{"results": [...]}`. Es la planta de muestra cuando no hay base."""
    items = cuerpo.get("results", cuerpo) if isinstance(cuerpo, dict) else cuerpo
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and (ids := ids_enteros([item.get("id")])):
            return ids[0]
    return None


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
        parser.add_argument(
            "--sin-base", action="store_true",
            help="No lee la base: la planta de muestra sale del listado de SolarView.",
        )
        parser.add_argument(
            "--id-solarview", type=int,
            help="Planta de muestra en api.sole.tech (si no, la de la base o la del listado).",
        )

    def handle(self, *args, **opciones):
        from apps.comun.config import settings
        from apps.plataforma.services.fechas import hoy_col
        from apps.proyectos.models import Proyecto

        hoy = hoy_col()
        ayer = hoy - timedelta(days=1)
        muestra_solarview = opciones["id_solarview"]

        # La base solo hace falta para lo que no vino por argumento: la planta
        # de muestra, o la flota entera con --flota. Sin ella (en local, por
        # ejemplo), la muestra sale del primer proyecto del listado de SolarView.
        ids_solarview: list[int] = []
        if opciones["sin_base"]:
            if opciones["flota"]:
                self.stdout.write(self.style.WARNING("  --flota necesita la base: no se mide"))
                opciones["flota"] = False
        elif muestra_solarview is None or opciones["flota"]:
            en_operacion = Proyecto.objects.filter(estado="en_operacion", deleted_at__isnull=True)
            ids_solarview = ids_enteros(en_operacion.values_list("project_id_solarview", flat=True))
            if muestra_solarview is None and ids_solarview:
                muestra_solarview = ids_solarview[0]

        medidas: list[Medida] = []
        with httpx.Client(timeout=opciones["timeout"], follow_redirects=True) as http:
            medidas += self._otros_hosts(http, settings)
            medidas += self._solarview(http, settings, muestra_solarview, ayer, hoy)
            if opciones["flota"]:
                self._flota(http, settings, ids_solarview, ayer, hoy)

        self._imprimir(medidas)

    # ── data.sole.tech (Solenium) y sunfactory.sole.tech ────────────────────

    def _otros_hosts(self, http, settings) -> list[Medida]:
        from apps.comun import sole_tech

        self._titulo("data.sole.tech (Solenium) y sunfactory.sole.tech")
        if not sole_tech.configurado():
            self.stdout.write(self.style.WARNING("  SOLARVIEW_TOKEN vacío en este .env"))
            return []

        # Alarmas, inversores y reconectadores ya leen de SolarView. De
        # data.sole.tech solo queda el ON/OFF de los reconectadores (con las
        # credenciales de quien aprieta el botón), y Sun Factory sigue con su
        # login: con el token, los dos responden 401. Se prueban para ver el
        # día que eso cambie.
        data = settings.SOLENIUM_DATA_URL.rstrip("/")
        sunfactory = settings.SUNFACTORY_API_URL.rstrip("/")
        llamadas = [
            ("data.sole.tech con el token (hoy solo lo usa el ON/OFF)", f"{data}/project/", {"menu": 1}),
            ("sunfactory.sole.tech con el token (usa su propio login)",
             f"{sunfactory}/project/", {"limit": 1}),
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
        listado, cuerpo = medir(
            http, "proyectos de la compañía", "GET",
            f"{base}/solarview/config/company-projects/", headers=cabeceras,
        )
        sv_id = sv_id or primer_id(cuerpo)
        llamadas = [
            ("disponibilidad de la flota", f"{base}/solarview/kpis/availability/", None),
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
                # Un 404 acá es "la planta no tiene reconectador", no una falla:
                # 14 de las 39 plantas de SolarView no tienen (2026-09-29).
                # El estado y los eventos piden `project_id`; el histórico,
                # `recloser` -- los dos con el id del proyecto.
                (f"estado del reconectador de {sv_id} (reconectadores)",
                 f"{base}/solarview/config/recloser/", {"project_id": sv_id}),
                (f"eventos del reconectador de {sv_id}",
                 f"{base}/solarview/config/recloser/events/", {"project_id": sv_id}),
                (f"histórico de reconectador de {sv_id}",
                 f"{base}/solarview/config/recloser/historical/",
                 {"recloser": sv_id, "start_date": f"{hoy.isoformat()} 00:00:00",
                  "end_date": f"{hoy.isoformat()} 23:59:59", "vars": "kw"}),
            ]
        return [listado] + [
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
