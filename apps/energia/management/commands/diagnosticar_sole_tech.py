"""Qué responde hoy cada API de sole.tech que usa la plataforma, y cuánto tarda.

**Solo lee. No escribe nada, ni en la base ni en sole.tech.** El único POST es
el de pedir un token con usuario y contraseña (`auth.sole.tech/api/token/`),
que no cambia nada del otro lado.

Para qué. La plataforma entra a sole.tech por dos puertas:

  - **Usuario y contraseña** (`SOLENIUM_USER`/`SOLENIUM_PASS` contra
    `auth.sole.tech`, JWT para `data.sole.tech`): alarmas, inversores de
    monitoreo y de puesta en marcha, potencia del dashboard, reconectadores.
  - **Token** (`SOLARVIEW_TOKEN` contra `api.sole.tech`): generación solar,
    flota, detalle.

Antes de pasar a SolarView lo que sigue por la primera hay que saber qué está
muerto de verdad y qué no, y cuánto tarda cada endpoint. Desde local no se puede:
el `.env` local no trae esas credenciales.

Se corre en el servidor:

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


def _base_auth(url: str) -> str:
    """`SOLENIUM_AUTH_URL` llega con y sin `/token` al final; los clientes lo quitan igual."""
    return url.rstrip("/").removesuffix("/token")


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

    # ── Usuario y contraseña: auth.sole.tech + data.sole.tech ────────────────

    def _solenium(self, http, settings, sol_id) -> list[Medida]:
        self._titulo("Usuario y contraseña (auth.sole.tech → data.sole.tech)")
        usuario, clave = settings.SOLENIUM_USER, settings.SOLENIUM_PASS
        if not (usuario and clave):
            self.stdout.write(self.style.WARNING("  SOLENIUM_USER / SOLENIUM_PASS vacíos en este .env"))
            return []

        auth = _base_auth(settings.SOLENIUM_AUTH_URL)
        data = settings.SOLENIUM_DATA_URL.rstrip("/")
        login, cuerpo = medir(
            http, "login usuario/contraseña", "POST", f"{auth}/token/",
            json={"username": usuario, "password": clave},
        )
        medidas = [login]
        token = (cuerpo or {}).get("access") if login.ok and isinstance(cuerpo, dict) else None
        if not token:
            self.stdout.write(self.style.WARNING(
                "  Sin token: no se prueban los endpoints de data.sole.tech con JWT."
            ))
        else:
            cabeceras = {"Authorization": f"Bearer {token}"}
            llamadas = [
                ("proyectos (inversores de monitoreo)", f"{data}/project/", {"menu": 1}),
                ("disponibilidad (alarmas)", f"{data}/project_availability/", None),
                ("resumen de flota (dashboard)", f"{data}/project_summary/", None),
            ]
            if sol_id:
                llamadas += [
                    (f"inversores de {sol_id} (monitoreo / puesta en marcha)",
                     f"{data}/project/{sol_id}/inverter/", None),
                    (f"relay de {sol_id} (reconectadores)", f"{data}/project/{sol_id}/relay/", None),
                ]
            for que, url, params in llamadas:
                medidas.append(medir(http, que, "GET", url, headers=cabeceras, params=params)[0])

        # Si data.sole.tech aceptara el token de SolarView, la migración de
        # esos servicios sería solo cambiar la cabecera.
        if settings.SOLARVIEW_TOKEN:
            medidas.append(medir(
                http, "data.sole.tech con el TOKEN de SolarView", "GET", f"{data}/project/",
                headers={"Authorization": f"Token {settings.SOLARVIEW_TOKEN}"},
            )[0])
        return medidas

    # ── Token: api.sole.tech ────────────────────────────────────────────────

    def _solarview(self, http, settings, sv_id, ayer, hoy) -> list[Medida]:
        self._titulo("Token (api.sole.tech)")
        token = settings.SOLARVIEW_TOKEN
        if not token:
            self.stdout.write(self.style.WARNING("  SOLARVIEW_TOKEN vacío en este .env"))
            return []

        base = settings.SOLARVIEW_BASE_URL.rstrip("/")
        cabeceras = {"Authorization": f"Token {token}"}
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
        if not settings.SOLARVIEW_TOKEN:
            self.stdout.write(self.style.WARNING("  SOLARVIEW_TOKEN vacío: no se mide"))
            return
        url = f"{settings.SOLARVIEW_BASE_URL.rstrip('/')}/solarview/measurements/generation/"
        cabeceras = {"Authorization": f"Token {settings.SOLARVIEW_TOKEN}"}

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
