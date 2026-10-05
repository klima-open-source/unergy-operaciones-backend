"""Subida de los PDF de mandatos a Google Drive.

Port de `app/services/finanzas_mandatos_drive.py`. La conexión y las carpetas
son las de `apps/comun/drive_evidencia.py` (la versión común de ese código);
acá solo queda dónde van los mandatos. Sin Drive configurado levanta
`DriveNoConfigurado`, no un `HTTPException` de FastAPI: la vista lo traduce.
"""
from __future__ import annotations

import io
import os

from apps.comun import drive_evidencia

# Carpeta raíz en el shared drive donde se guardan los mandatos. Override por env.
DRIVE_MANDATOS_FOLDER_ID = os.environ.get(
    "DRIVE_MANDATOS_FOLDER_ID", "0AD_e3wIWHByDUk9PVA")


def subir_pdf(contenido: bytes, nombre: str, subcarpeta: str) -> dict:
    """Sube el PDF a DRIVE_MANDATOS_FOLDER_ID/subcarpeta. Devuelve {id, url}."""
    from googleapiclient.http import MediaIoBaseUpload

    service = drive_evidencia.servicio()
    folder_id = drive_evidencia.carpeta(service, subcarpeta, DRIVE_MANDATOS_FOLDER_ID)
    media = MediaIoBaseUpload(io.BytesIO(contenido), mimetype="application/pdf")
    up = service.files().create(
        body={"name": nombre, "parents": [folder_id]},
        media_body=media, fields="id, webViewLink", supportsAllDrives=True).execute()
    fid = up["id"]
    return {"id": fid, "url": up.get("webViewLink", f"https://drive.google.com/file/d/{fid}/view")}
