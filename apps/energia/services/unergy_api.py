"""Cliente de la API Unergy: lecturas de generación en vivo.

Lee su configuración de `os.environ` igual que `apps/contabilidad/services/drive.py`
— no hace falta plumbing en settings para cuatro variables que solo usa este
módulo.

`ponytail: httpx sincrónico + ThreadPoolExecutor, no async`. El original usa
`asyncio.gather` sobre los proyectos; acá el fan-out es un pool de hilos porque
las vistas de DRF son sincrónicas y mezclar los dos modelos por una llamada HTTP
no compra nada. Si el número de proyectos crece mucho, subir `HILOS` antes de
plantearse vistas async.
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger("operaciones.monitoreo")

# Colombia es UTC−5 sin horario de verano.
TZ_COL = timezone(timedelta(hours=-5))

HILOS = 8
TTL_TOKEN = 300           # 5 min; la API no dice cuánto vale el token
DIAS_DE_MARGEN = 2        # se piden 2 días extra para tener lectura previa

_token: dict = {"valor": "", "expira": 0.0}


def _env(nombre: str) -> str:
    """Del shim compartido, no de `os.environ` a secas: ahí viven los defaults
    que traía pydantic y que el `.env` nunca necesitó declarar (`UNERGY_API_URL`
    entre ellos). Leerlo directo devolvía "" y armaba una URL relativa."""
    from apps.comun.config import settings

    return getattr(settings, nombre)


def token() -> str:
    """Token de la API Unergy, reutilizado 5 minutos."""
    ahora = time.monotonic()
    if _token["valor"] and ahora < _token["expira"]:
        return _token["valor"]

    url = (
        f'{_env("UNERGY_API_URL")}/api/accounts/'
        f'{_env("UNERGY_ACCOUNT_ID")}/'
    )
    with httpx.Client(timeout=30) as http:
        respuesta = http.post(
            url,
            json={"login": _env("UNERGY_LOGIN"), "password": _env("UNERGY_PASSWORD")},
        )
        respuesta.raise_for_status()
        datos = respuesta.json()

    valor = datos.get("token") or datos.get("access") or datos.get("key") or ""
    if valor:
        _token["valor"], _token["expira"] = valor, ahora + TTL_TOKEN
    return valor


def lecturas_crudas(
    token_: str, sub_project: str, desde_iso: str, hasta_iso: str,
    solo_verificadas: bool,
) -> list:
    params = {
        "time_stamp__gte": desde_iso,
        "time_stamp__lte": hasta_iso,
        "sub_project": sub_project,
        "limit": "10000",
    }
    if solo_verificadas:
        params["verified_by_operator"] = "True"

    url = f'{_env("UNERGY_API_URL")}/api/admin/operations/project_generation/'
    with httpx.Client(timeout=60, follow_redirects=True) as http:
        respuesta = http.get(
            url, params=params, headers={"Authorization": f"Bearer {token_}"}
        )
        if respuesta.status_code == 401:
            return []
        respuesta.raise_for_status()
        cuerpo = respuesta.json()
    return cuerpo if isinstance(cuerpo, list) else cuerpo.get("results", [])


def lecturas_con_respaldo(
    token_, sub_project, desde_iso, hasta_iso
) -> tuple[list, str]:
    """`(lecturas, fuente)`. Pide las verificadas y, si no hay ninguna, cae a todas.

    `verified_by_operator` es un campo de la API de Unergy --no nuestro, no hay
    columna equivalente en ninguna base a la que lleguemos-- que marca las
    lecturas que alguien reviso. El respaldo existe porque hay plantas sin nadie
    verificando: sin el, su gráfica saldría vacía en vez de mostrar el dato
    crudo.

    **La fuente se devuelve porque las dos no son lo mismo y la diferencia no es
    teórica.** Medido contra la API el 2026-09-12, sobre 20 plantas y seis
    semanas: 19 tenían lecturas verificadas y una (GD Delta 2) ninguna de sus
    1.002. Un grupo estaba al 100% y otro cerca del 74%, o sea que a esas se les
    descarta una cuarta parte de lo que reportó el medidor. Dos plantas del
    mismo sitio pueden salir una depurada y la otra cruda en el mismo gráfico, y
    hasta ahora nada lo decía.

    `"sin_datos"` cuando no hay ninguna lectura: no es lo mismo que "crudas y
    vacías", y quien dibuje tiene que poder distinguirlo.
    """
    lecturas = lecturas_crudas(token_, sub_project, desde_iso, hasta_iso, True)
    if lecturas:
        return lecturas, "verificada"
    lecturas = lecturas_crudas(token_, sub_project, desde_iso, hasta_iso, False)
    return lecturas, ("cruda" if lecturas else "sin_datos")


def deltas(lecturas: list, desde_dt: datetime, hasta_dt: datetime) -> list[dict]:
    """Convierte el contador acumulado en kWh por intervalo.

    La API devuelve `generacion` como un contador que solo sube, así que el
    consumo del intervalo es la diferencia con la lectura anterior. Se incluye
    UNA lectura previa al rango (de ahí los 2 días de margen) para que el primer
    intervalo del período no salga a cero.
    """
    lecturas.sort(key=lambda l: l.get("time_stamp") or l.get("timestamp") or "")
    previas, dentro = [], []
    for lectura in lecturas:
        momento = _fecha_de(lectura)
        if momento is None:
            continue
        if momento < desde_dt:
            previas.append((momento, lectura))
        elif momento <= hasta_dt:
            dentro.append((momento, lectura))

    if not dentro:
        return []

    serie = ([previas[-1]] if previas else []) + dentro
    salida = []
    for i in range(1, len(serie)):
        _, anterior = serie[i - 1]
        momento, actual = serie[i]
        # `max(0, …)` porque un reinicio del medidor daría una diferencia
        # negativa que no es generación.
        delta = max(0.0, _generacion(actual) - _generacion(anterior))
        local = momento.astimezone(TZ_COL)
        salida.append({
            "time": local.strftime("%Y-%m-%d %H:%M"),
            "date": local.strftime("%Y-%m-%d"),
            "kwh": round(delta, 3),
        })
    return salida


def _fecha_de(lectura: dict) -> datetime | None:
    crudo = lectura.get("time_stamp") or lectura.get("timestamp") or ""
    try:
        if "T" in crudo:
            return datetime.fromisoformat(crudo.replace("Z", "+00:00"))
        return datetime.strptime(crudo[:16], "%Y-%m-%d %H:%M").replace(tzinfo=TZ_COL)
    except Exception:
        return None


def _generacion(lectura: dict) -> float:
    return float(lectura.get("generacion") or lectura.get("generation") or 0)


def ventana_utc(desde, hasta) -> tuple[str, str]:
    """El rango de fechas locales, en ISO UTC y con el margen para el delta."""
    desde_dt = datetime(desde.year, desde.month, desde.day, 0, 0, 0, tzinfo=TZ_COL)
    hasta_dt = datetime(hasta.year, hasta.month, hasta.day, 23, 59, 59, tzinfo=TZ_COL)
    pedir_desde = desde_dt - timedelta(days=DIAS_DE_MARGEN)
    return (
        pedir_desde.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        hasta_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    ), (desde_dt, hasta_dt)


def generacion_de_la_flota(proyectos, desde, hasta) -> dict:
    """Generación real de todos los proyectos, agregada por fecha y por proyecto."""
    (pedir_desde, pedir_hasta), (desde_dt, hasta_dt) = ventana_utc(desde, hasta)
    try:
        token_ = token()
    except Exception:
        logger.warning("no se pudo obtener token de la API Unergy", exc_info=True)
        return {
            "projects_count": len(proyectos), "dates": [], "by_project": [],
            "error": "token_error",
        }

    def uno(proyecto):
        try:
            # La fuente no se usa acá: este endpoint agrega la flota entera
            # en una curva sola, donde etiquetar planta por planta no cabe.
            lecturas, _fuente = lecturas_con_respaldo(
                token_, proyecto.sub_project, pedir_desde, pedir_hasta
            )
            return proyecto, deltas(lecturas, desde_dt, hasta_dt)
        except Exception:
            # Un proyecto que falle no debe vaciar la gráfica de los demás.
            logger.debug(
                "sin lecturas para sub_project=%s", proyecto.sub_project,
                exc_info=True,
            )
            return proyecto, []

    with ThreadPoolExecutor(max_workers=HILOS) as pool:
        resultados = list(pool.map(uno, proyectos))

    por_fecha: dict[str, float] = {}
    por_proyecto: list[dict] = []
    for proyecto, entradas in resultados:
        total = 0.0
        for entrada in entradas:
            fecha, kwh = entrada.get("date", ""), float(entrada.get("kwh") or 0)
            if fecha:
                por_fecha[fecha] = por_fecha.get(fecha, 0.0) + kwh
                total += kwh
        por_proyecto.append({
            "proyecto_id": proyecto.id,
            "nombre": proyecto.nombre_comercial,
            "sub_project": proyecto.sub_project,
            "kwh_real": round(total, 1),
        })

    return {
        "projects_count": len(proyectos),
        "dates": [
            {"fecha": f, "kwh_real": round(v, 1)}
            for f, v in sorted(por_fecha.items())
        ],
        "by_project": sorted(
            por_proyecto, key=lambda p: p["kwh_real"], reverse=True
        ),
    }
