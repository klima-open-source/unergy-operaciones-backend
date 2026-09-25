"""Acceso a Google Drive para listar carpetas.

Los helpers de subida a Drive viven duplicados en `api/v1/fallas.py`,
`api/v1/costos_variables.py` y `api/v1/panel_contable.py`. Este módulo NO los
reemplaza (migrarlos es una limpieza aparte); cubre el caso que ninguno resuelve:
**listar** el contenido de una carpeta para mostrarlo en la plataforma.

Sin dependencias de FastAPI — los errores se traducen a HTTP en el endpoint.

Portado tal cual desde `app/services/drive.py`: lee su configuracion de
`os.environ` (`GOOGLE_SERVICE_ACCOUNT_JSON`, `DRIVE_ER_FOLDER_ID`) y no toca ni
la base ni el framework, asi que el port fue una copia sin cambios.
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from threading import Lock, local
from zipfile import ZIP_DEFLATED, ZipFile

# La carpeta "Prod" de estados de resultados vive en un shared drive DISTINTO al
# `DRIVE_ROOT_FOLDER_ID` de fallas.py, por eso se configura aparte.
DRIVE_ER_FOLDER_ID_DEFAULT = "1_22PQ3sJQBIq5bSxgVJ5nxWruIuk_nSs"

_SCOPES = ["https://www.googleapis.com/auth/drive"]
_PAGE_SIZE = 1000  # máximo que acepta files.list

# La carpeta tiene ~1.700 archivos y cambia poco (se llena al cerrar el mes), así
# que se cachea el listado en memoria para no pagar 2 round-trips a Drive por request.
_CACHE_TTL_S = 300
_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = Lock()


class DriveNoConfigurado(RuntimeError):
    """Falta GOOGLE_SERVICE_ACCOUNT_JSON en el entorno."""


class DriveSinAcceso(RuntimeError):
    """La carpeta no existe o el service account no tiene permiso sobre ella."""


def er_folder_id() -> str:
    return os.environ.get("DRIVE_ER_FOLDER_ID") or DRIVE_ER_FOLDER_ID_DEFAULT


def get_drive_service():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise DriveNoConfigurado("falta GOOGLE_SERVICE_ACCOUNT_JSON")
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ── Parseo de nombres ────────────────────────────────────────────────────────────
# La carpeta guarda los DOS artefactos que genera la vista de Estados de resultados:
#   `Estado resultados {CLIENTE} {PROYECTO} {MES} {AÑO}.xlsx`   (uno por proyecto)
#   `Cruce facturas {MES} {AÑO} {VERSION}.xlsx`                 (uno por período)
#
# En el ER el `.+?` es no-greedy pero el ancla `$` fuerza a que mes/año sean los DOS
# ÚLTIMOS tokens, así que un proyecto que termina en dígito ("Chiriguaná Norte 4") no
# se confunde con el mes.
#
# El prefijo opcional "Copia de " lo pone Drive al duplicar; ~100 archivos lo tienen y
# son legítimos, así que se aceptan (marcados como copia) en vez de quedar fuera de
# todo filtro por período.
_COPIA = r"^(?P<copia>Copia\s+de\s+)?"

# La version al final es OPCIONAL: hoy la API no la pone en el nombre del ER
# --verificado el 2026-09-25 sobre los 2.309 archivos de la carpeta y pidiendo
# por id los que se generaron ese dia: ninguno la trae, y tampoco esta dentro
# del xlsx-- pero si la pone en el del cruce. Se acepta desde ya para que el dia
# que la agreguen la vista la muestre sin tocar nada.
_RE_ER = re.compile(
    _COPIA + r"Estado\s+resultados\s+(?P<desc>.+?)"
    r"\s+(?P<mes>\d{1,2})\s+(?P<anio>\d{4})"
    r"(?:\s+(?P<version>txf|txr|tx[2-8]))?\.xlsx?$",
    re.IGNORECASE,
)
_RE_CRUCE = re.compile(
    _COPIA + r"Cruce\s+facturas\s+(?P<mes>\d{1,2})\s+(?P<anio>\d{4})"
    r"\s+(?P<version>[\w.-]+)\.xlsx?$",
    re.IGNORECASE,
)

TIPO_ER = "estado_resultados"
TIPO_CRUCE = "cruce_facturas"
TIPO_OTRO = "otro"


def parse_nombre_er(nombre: str) -> dict:
    """Deduce (tipo, mes, anio, descripcion, version, es_copia) del nombre del archivo.

    Un nombre fuera de convención no es un error: se devuelve como tipo "otro" sin
    período y con el nombre completo como descripción, para que se siga listando.
    """
    nombre = nombre or ""
    desconocido = {
        "tipo": TIPO_OTRO, "mes": None, "anio": None,
        "descripcion": nombre, "version": None, "es_copia": False,
    }

    m = _RE_ER.match(nombre)
    if m:
        mes = int(m.group("mes"))
        if 1 <= mes <= 12:
            return {
                "tipo": TIPO_ER,
                "mes": mes,
                "anio": int(m.group("anio")),
                "descripcion": m.group("desc"),
                # Hoy viene vacía: la API no la escribe en el nombre del ER.
                "version": (m.group("version") or "").lower() or None,
                "es_copia": bool(m.group("copia")),
            }
        return desconocido

    m = _RE_CRUCE.match(nombre)
    if m:
        mes = int(m.group("mes"))
        if 1 <= mes <= 12:
            return {
                "tipo": TIPO_CRUCE,
                "mes": mes,
                "anio": int(m.group("anio")),
                # El cruce es del período completo: no tiene cliente ni proyecto.
                "descripcion": "",
                "version": m.group("version"),
                "es_copia": bool(m.group("copia")),
            }
    return desconocido


# ── Listado ──────────────────────────────────────────────────────────────────────
def _fetch_folder(folder_id: str) -> list[dict]:
    service = get_drive_service()
    from googleapiclient.errors import HttpError

    archivos: list[dict] = []
    token = None
    try:
        while True:
            resp = (
                service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields="nextPageToken, files(id,name,mimeType,modifiedTime,size,webViewLink)",
                    orderBy="modifiedTime desc",
                    pageSize=_PAGE_SIZE,
                    pageToken=token,
                    # Obligatorios: la carpeta vive en un shared drive; sin esto
                    # Drive devuelve una lista vacía en vez de un error.
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            archivos.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                break
    except HttpError as e:
        if e.resp.status in (403, 404):
            raise DriveSinAcceso(
                "el service account no tiene acceso a la carpeta de Drive"
            ) from e
        raise
    return archivos


def listar_carpeta(folder_id: str, usar_cache: bool = True) -> list[dict]:
    """Todos los archivos de la carpeta (paginado), cacheados por `_CACHE_TTL_S`."""
    ahora = time.monotonic()
    if usar_cache:
        with _cache_lock:
            hit = _cache.get(folder_id)
            if hit and ahora - hit[0] < _CACHE_TTL_S:
                return hit[1]
    archivos = _fetch_folder(folder_id)
    with _cache_lock:
        _cache[folder_id] = (ahora, archivos)
    return archivos


def filtrar_archivos(
    archivos: list[dict],
    tipo: str | None = None,
    mes: int | None = None,
    anio: int | None = None,
    version: str | None = None,
    q: str | None = None,
) -> list[dict]:
    """Aplica los filtros del listado. Función pura: no muta `archivos`.

    La usan el listado y la descarga masiva, para que el ZIP traiga exactamente
    los archivos que el usuario está viendo en la tabla.
    """
    res = list(archivos)
    if tipo:
        res = [a for a in res if a.get("tipo") == tipo]
    if mes is not None:
        res = [a for a in res if a.get("mes") == mes]
    if anio is not None:
        res = [a for a in res if a.get("anio") == anio]
    if version:
        v = version.strip().lower()
        res = [a for a in res if (a.get("version") or "").lower() == v]
    if q:
        needle = q.strip().lower()
        res = [a for a in res if needle in (a.get("name") or "").lower()]
    return res


# ── Descarga ─────────────────────────────────────────────────────────────────────
# Los objetos service de googleapiclient NO son thread-safe, así que cada hilo del
# pool construye el suyo.
_local = local()


def _service_del_hilo():
    if not hasattr(_local, "service"):
        _local.service = get_drive_service()
    return _local.service


def descargar_archivo(file_id: str) -> bytes:
    """Contenido binario del archivo. Los ER son .xlsx reales (no Sheets nativos),
    así que se bajan con get_media y no hace falta exportar."""
    from googleapiclient.errors import HttpError

    try:
        return _service_del_hilo().files().get_media(fileId=file_id).execute()
    except HttpError as e:
        if e.resp.status in (403, 404):
            raise DriveSinAcceso(f"sin acceso al archivo {file_id}") from e
        raise


def construir_zip(archivos: list[dict], max_workers: int = 8) -> bytes:
    """ZIP en memoria con los archivos dados (cada dict con 'id' y 'name').

    Cabe en memoria de sobra: los ER pesan ~9 KB y la carpeta completa ~16 MB.
    Se baja en paralelo porque el costo es latencia de red, no CPU. Un archivo que
    falle no tumba el ZIP: se omite y se reporta en `_errores.txt`.
    """
    buf = BytesIO()
    fallidos: list[str] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futuros = {pool.submit(descargar_archivo, a["id"]): a for a in archivos}
        contenidos: list[tuple[str, bytes]] = []
        for fut in as_completed(futuros):
            a = futuros[fut]
            try:
                contenidos.append((a.get("name") or a["id"], fut.result()))
            except Exception as e:  # noqa: BLE001 — un archivo roto no invalida el resto
                fallidos.append(f"{a.get('name') or a['id']}: {e}")

    with ZipFile(buf, "w", ZIP_DEFLATED) as z:
        usados: dict[str, int] = {}
        for nombre, data in sorted(contenidos, key=lambda c: c[0]):
            # La carpeta tiene nombres repetidos (p. ej. 14 "Cruce facturas 1 2026
            # txf.xlsx"); dentro del ZIP hay que desambiguarlos o se sobreescriben.
            n = usados.get(nombre, 0)
            usados[nombre] = n + 1
            final = nombre if n == 0 else _sufijar(nombre, n + 1)
            z.writestr(final, data)
        if fallidos:
            z.writestr("_errores.txt", "\n".join(fallidos))

    return buf.getvalue()


def _sufijar(nombre: str, n: int) -> str:
    base, punto, ext = nombre.rpartition(".")
    return f"{base} ({n}).{ext}" if punto else f"{nombre} ({n})"


def invalidar_cache(folder_id: str | None = None) -> None:
    with _cache_lock:
        if folder_id:
            _cache.pop(folder_id, None)
        else:
            _cache.clear()
