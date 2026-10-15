"""Lógica pura de mandatos: los CMU y el nombre de archivo de un mandato.

La usa la lectura del correo (`correo_sync`, `email_parser`), que llena
`finanzas_mandatos`. Las reglas de la vista vieja de Costos › Mandatos (estados,
resumen, emparejar inversionista) se fueron con ella el 2026-10-13.
"""
from __future__ import annotations
import re

CMU_RE = re.compile(r"CMU\d+")
# Tres convenciones reales conviven (verificadas contra la bitácora de la
# primera corrida, 2026-08-19):
#   CMU0988-Mandato-Costos-{Proyecto}-{Inversionista}.pdf   costos, con inversionista
#   CMU1140-Mandato-Costos-{Proyecto}.pdf                   costos, mandante es un P.A.
#   CMU1182-Mandato-{Proyecto}.pdf                          ingresos/autoconsumo
#
# "Costos-" es opcional; su ausencia es justamente lo que distingue un mandato
# de ingresos (ver tipo_de_nombre). El inversionista también es opcional, y se
# reconoce por el ESPACIADO del guion: pegado cuando separa al inversionista
# ("...Uruaco-SUNO..."), con espacios cuando es parte del nombre del proyecto
# ("PSF - Yurbaqua"). Heurística, no garantía -- documentada en el spec.
ZIP_NOMBRE_RE = re.compile(
    r"^(CMU\d+)-Mandato-(?:Costos-)?(.+?)(?:(?<! )-(?! )([^-]+))?\.pdf$",
    re.IGNORECASE)

# Gmail renombra los adjuntos repetidos agregando " (1)", " (2)". Sin quitarlo,
# el sufijo termina dentro del nombre del inversionista y la misma entidad
# genera dos identidades distintas.
_SUFIJO_GMAIL_RE = re.compile(r"\s\(\d+\)(?=\.pdf$)", re.IGNORECASE)

# Cuarta convención, vista en el lote real de Sol de la Sierra (2026-08-20):
# "...-{Inversionista} - 17844 SOL DE LA SIERRA.pdf". El P.A. va al final,
# precedido de guion CON espacios y encabezado por su código numérico.
#
# Sin quitarlo antes, la regla principal no encuentra dónde cortar --  el
# inversionista no puede contener guiones y el separador espaciado no cumple
# los lookarounds -- así que el proyecto se tragaba TODO el resto:
# "Minigranja Solar Merengue-PATRIMONIOS AUTONOMOS ... - 17844 SOL DE LA
# SIERRA". Esa identidad falsa creaba una fila fantasma por cada mandato.
#
# Se ancla al CÓDIGO numérico y no al guion espaciado a propósito: un proyecto
# sí puede llevar " - " en su nombre ("PSF - Yurbaqua"), y recortar por el
# guion solo le comería el nombre.
_SUFIJO_PA_RE = re.compile(r"\s-\s(\d{4,6})\s+(.+?)(?=\.pdf$)", re.IGNORECASE)

def extraer_cmus(texto: str) -> list[str]:
    """Todos los CMU del texto, en orden de aparición, sin duplicados."""
    vistos: list[str] = []
    for m in CMU_RE.findall(texto or ""):
        if m not in vistos:
            vistos.append(m)
    return vistos


def parsear_nombre_zip(nombre: str) -> dict | None:
    """'CMU0988-Mandato-Costos-{Proyecto}[-{Inversionista}].pdf' → dict | None.

    El inversionista es opcional: los mandatos de un P.A. de fiduciaria no lo
    traen en el nombre (ahí el mandante sale del cuerpo del correo). Cuando
    falta, se devuelve cadena vacía -- nunca se inventa.

    Devuelve además `pa_codigo` y `pa_nombre` cuando el nombre trae el P.A. al
    final (ver _SUFIJO_PA_RE); son cadena vacía cuando no.
    """
    limpio = _SUFIJO_GMAIL_RE.sub("", (nombre or "").strip())
    pa_codigo = pa_nombre = ""
    pa = _SUFIJO_PA_RE.search(limpio)
    if pa:
        pa_codigo, pa_nombre = pa.group(1), pa.group(2).strip()
        limpio = limpio[:pa.start()] + ".pdf"
    m = ZIP_NOMBRE_RE.match(limpio)
    if not m:
        return None
    return {"cmu": m.group(1).upper(),
            "proyecto": (m.group(2) or "").strip(),
            "inversionista": (m.group(3) or "").strip(),
            "pa_codigo": pa_codigo, "pa_nombre": pa_nombre}
