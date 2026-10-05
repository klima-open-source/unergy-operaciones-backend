"""Expansión de adjuntos de correo: un ZIP rinde los PDFs que trae adentro.

La revisoría manda los mandatos firmados dentro de un ZIP, con la misma
convención de nombres que los sueltos. Para el resto del sistema no debería
haber diferencia entre "vino en un ZIP" y "vino suelto", así que se aplana acá.
"""
from __future__ import annotations

import io
import logging
import zipfile
from pathlib import PurePosixPath

logger = logging.getLogger("mandatos.adjuntos")

# Un ZIP de mandatos ronda unos pocos MB. El tope evita que un adjunto absurdo
# se descomprima en memoria dentro del cron.
_MAX_DESCOMPRIMIDO = 50 * 1024 * 1024


def expandir_adjuntos(adjuntos: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """[(nombre, contenido)] con los ZIP reemplazados por los PDFs de adentro.

    Los nombres se aplanan: `julio/CMU1-....pdf` sale como `CMU1-....pdf`, porque
    el resto del sistema parsea el nombre del archivo y no le sirve la ruta.

    Un ZIP corrupto se descarta con un log y no interrumpe los demás adjuntos:
    perder un adjunto ilegible es preferible a perder toda la corrida.
    """
    salida: list[tuple[str, bytes]] = []
    for nombre, contenido in adjuntos:
        if not nombre.lower().endswith(".zip"):
            salida.append((nombre, contenido))
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(contenido)) as zf:
                total = sum(i.file_size for i in zf.infolist())
                if total > _MAX_DESCOMPRIMIDO:
                    logger.warning("Adjuntos: %r descomprime a %d bytes, se omite",
                                   nombre, total)
                    continue
                for info in zf.infolist():
                    if info.is_dir() or not info.filename.lower().endswith(".pdf"):
                        continue
                    salida.append((PurePosixPath(info.filename).name, zf.read(info)))
        except Exception as exc:
            logger.warning("Adjuntos: no se pudo abrir %r: %s", nombre, exc)
    return salida
