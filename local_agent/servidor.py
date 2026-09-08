"""Agente local de Descarga de XM.

Corre en el computador de la usuaria (no en Railway) porque el FTP de XM
solo acepta conexiones desde IPs conocidas — Railway no puede llegar ahí
directo (ver docs/superpowers/specs/2026-07-02-descarga-xm-design.md,
adenda de pivote a agente local).

La pestaña "Descarga de XM" del frontend (servido desde Vercel/Railway)
llama a este servicio en http://127.0.0.1:8420 directamente desde el
navegador de la usuaria — el navegador SÍ puede llamar a localhost aunque
la página esté en HTTPS. Este proceso hace la conexión real a XM.

Uso: doble clic en iniciar_descarga_xm.bat, dejar la ventana abierta
mientras se usa la pestaña, cerrar cuando se termine.
"""
import io
import logging
import os
import sys
import threading
from pathlib import Path

# Permite correr este archivo directamente (python local_agent/app.py)
# reutilizando app.services.xm.* del backend en el mismo repo.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Carga local_agent/.env si existe — cualquiera del equipo puede fijar ahí
# su propia XM_CACHE_DIR sin tocar variables de entorno de Windows. Sin
# .env, cada quien recibe un default sensato según su usuario (ver
# app/services/xm/cache_local.py).
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.schemas.xm_descargas import XMDescargaRequest, XMJobResponse, XMJobStatus
from app.services.xm import jobs, tipos
from app.services.xm.orquestador import ejecutar_job

app = FastAPI(title="Agente local — Descarga de XM")

# Solo estos orígenes pueden llamar al agente — restringe qué páginas web
# pueden hacerle pedir a tu computador que se conecte a XM.
#
# El front desplegado cambia de dirección (Vercel → Cloudflare Workers, y antes
# de eso Railway) y cada vez que cambia, el agente deja de responderle con un
# 400 "Disallowed CORS origin" que en la pestaña se ve igual que si el .bat
# estuviera cerrado. Por eso la dirección del front desplegado se pone en
# `XM_ORIGENES_EXTRA` (en `local_agent/.env`, separadas por coma) en vez de
# quedar clavada aquí.
ORIGENES_PERMITIDOS = [
    "https://frontend-taupe-six-252g9aw47x.vercel.app",
    *[o.strip() for o in os.getenv("XM_ORIGENES_EXTRA", "").split(",") if o.strip()],
]

# Cualquier puerto de localhost: el front legacy corre en 5173 (Vite), el v2 en
# 3000 (Nuxt) y las builds de previsualización en 4173. Enumerarlos uno por uno
# ya costó una pestaña rota cuando el front se migró a Nuxt.
REGEX_LOCALHOST = r"http://(localhost|127\.0\.0\.1):\d+"

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGENES_PERMITIDOS,
    allow_origin_regex=REGEX_LOCALHOST,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

EXTENSIONES_VALIDAS = {"txf", "txr", "tx1", "tx2", "tx3", "tx4", "tx5", "tx6", "tx7", "tx8"}


@app.get("/salud")
def salud():
    return {"status": "ok"}


@app.post("/descargas", response_model=XMJobResponse)
def iniciar_descarga(body: XMDescargaRequest):
    if body.tipo not in tipos.TIPOS_CONFIG:
        raise HTTPException(400, f"Tipo de archivo no soportado: {body.tipo}")
    if body.extension.lower() not in EXTENSIONES_VALIDAS:
        raise HTTPException(400, f"Extensión no soportada: {body.extension}")
    if body.fecha_fin < body.fecha_inicio:
        raise HTTPException(400, "fecha_fin no puede ser anterior a fecha_inicio")
    if body.agente_filtro not in tipos.AGENTES_VALIDOS:
        raise HTTPException(400, f"Agente no soportado: {body.agente_filtro}")

    job_id = jobs.crear_job()
    ftp_params = {"host": body.ftp_host, "usuario": body.ftp_usuario, "clave": body.ftp_clave}

    hilo = threading.Thread(
        target=ejecutar_job,
        args=(job_id, ftp_params, body.tipo, body.extension, body.fecha_inicio,
              body.fecha_fin, body.enriquecer, body.agente_filtro),
        daemon=True,
    )
    hilo.start()
    return XMJobResponse(job_id=job_id)


@app.get("/descargas/{job_id}", response_model=XMJobStatus)
def estado_descarga(job_id: str):
    job = jobs.obtener_job(job_id)
    if job is None:
        raise HTTPException(404, "Job no encontrado o expirado")
    campos = {k: v for k, v in job.items() if k not in ("resultado", "creado_en")}
    return XMJobStatus(job_id=job_id, **campos)


@app.get("/descargas/{job_id}/archivo")
def descargar_archivo(job_id: str, formato: str = "xlsx"):
    job = jobs.obtener_job(job_id)
    if job is None:
        raise HTTPException(404, "Job no encontrado o expirado")
    if job["estado"] != "listo":
        raise HTTPException(409, f"El job aún no está listo (estado actual: {job['estado']})")

    resultado = job["resultado"]
    if formato == "xlsx":
        contenido, nombre = resultado["bytes_xlsx"], resultado["nombre_xlsx"]
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif formato == "txt":
        contenido, nombre = resultado["bytes_txt"], resultado["nombre_txt"]
        media_type = "text/plain"
    else:
        raise HTTPException(400, "formato debe ser 'xlsx' o 'txt'")

    return StreamingResponse(
        io.BytesIO(contenido), media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


def _silenciar_connection_reset_windows(loop, contexto):
    """El ProactorEventLoop de Windows imprime un ConnectionResetError
    inofensivo cuando el navegador cierra la conexión justo después de
    recibir un archivo — la respuesta ya se entregó completa antes de
    eso. Se silencia solo ese caso puntual; cualquier otro error se
    reporta normal."""
    excepcion = contexto.get("exception")
    if isinstance(excepcion, ConnectionResetError):
        return
    loop.default_exception_handler(contexto)


if __name__ == "__main__":
    import asyncio

    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # Los logs de acceso HTTP de uvicorn (una línea por cada GET de polling)
    # ahogarían los logs útiles de la descarga — se bajan a WARNING.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(_silenciar_connection_reset_windows)

    print("Agente local de Descarga de XM — escuchando en http://127.0.0.1:8420")
    print("Deja esta ventana abierta mientras usas la pestaña 'Descarga de XM'.")
    # Si la pestaña dice "No se pudo conectar", lo primero que hay que mirar es
    # si la dirección del front está en esta lista: un origen no permitido se ve
    # exactamente igual que el agente apagado.
    print(f"Páginas permitidas: {', '.join(ORIGENES_PERMITIDOS)} + cualquier localhost")
    config = uvicorn.Config(app, host="127.0.0.1", port=8420)
    server = uvicorn.Server(config)
    loop.run_until_complete(server.serve())
