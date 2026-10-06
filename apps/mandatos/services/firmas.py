"""¿Trae este PDF de mandato las dos firmas?

Verificado sobre un PDF real (CMU1287, 2026-08-13): las firmas NO son digitales
-- el documento no tiene AcroForm, ni campos /Sig, ni anotaciones -- y tampoco
son texto, porque los nombres y cargos vienen impresos en la plantilla. Son
IMÁGENES pegadas encima de las líneas `_____`.

Por eso el detector se ancla a las líneas de firma que encuentra en el texto, no
a coordenadas fijas: si la plantilla cambia de márgenes sigue funcionando.

La decisión (lineas_firmadas) está separada de la lectura del PDF
(verificar_firmas) para poder probarla con las coordenadas reales sin versionar
un documento financiero real.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("mandatos.firmas")

# Una línea de firma: cinco guiones bajos o más.
_LINEA_FIRMA_RE = re.compile(r"^_{5,}$")

# Cuánto por encima de la línea puede empezar la imagen de la firma. En el PDF
# real las firmas empiezan 29 y 26 pt arriba; 70 deja margen para plantillas algo
# distintas sin llegar a alcanzar el membrete, que está a 629 pt de distancia.
_TOLERANCIA_VERTICAL = 70


def lineas_firmadas(lineas: list[dict], imagenes: list[dict],
                    tolerancia: int = _TOLERANCIA_VERTICAL) -> list[bool]:
    """Por cada línea de firma, si hay una imagen encima que la firme.

    `lineas` e `imagenes` son dicts con x0/x1/top, tal como los da pdfplumber.
    Una línea cuenta como firmada si existe una imagen que:
      - se solapa con ella horizontalmente, y
      - empieza por encima de la línea, dentro de `tolerancia` puntos.

    Las dos condiciones son necesarias: el membrete y el pie de página TAMBIÉN se
    solapan en horizontal (el pie ocupa el 102% del ancho), y solo la condición
    vertical los deja fuera.
    """
    resultado: list[bool] = []
    for ln in lineas:
        firmada = any(
            im["x1"] > ln["x0"] and im["x0"] < ln["x1"]
            and im["top"] < ln["top"]
            and (ln["top"] - im["top"]) <= tolerancia
            for im in imagenes
        )
        resultado.append(firmada)
    return resultado


def resumir_firmas(firmadas: list[bool]) -> dict:
    """{'lineas': 2, 'firmadas': 2, 'estado': 'firmado_completo'}

    `estado` distingue cuatro casos, y la distinción importa:
      firmado_completo  todas las líneas tienen firma
      parcial           algunas sí, otras no
      sin_firmas        hay líneas y ninguna está firmada
      no_verificable    NO se encontraron líneas -- no es lo mismo que sin firmar

    Confundir `no_verificable` con `sin_firmas` haría que un PDF con otra
    plantilla se reporte como no firmado, o que se dé por concluido algo que
    nadie llegó a mirar.
    """
    total = len(firmadas)
    n = sum(firmadas)
    if total == 0:
        estado = "no_verificable"
    elif n == total:
        estado = "firmado_completo"
    elif n == 0:
        estado = "sin_firmas"
    else:
        estado = "parcial"
    return {"lineas": total, "firmadas": n, "estado": estado}


def verificar_firmas(contenido: bytes | None) -> dict:
    """Abre el PDF y devuelve el resumen de resumir_firmas().

    Recorre TODAS las páginas y acumula: la plantilla real tiene una sola, pero
    un mandato de varias hojas pondría las firmas en la última y buscar solo en
    la primera daría 'no_verificable' por error.

    Nunca lanza. Un adjunto corrupto, cifrado o que no es PDF devuelve
    `no_verificable`, nunca `sin_firmas`: no es lo mismo "este documento no está
    firmado" que "no pude abrirlo", y tratarlos igual convertiría un problema de
    lectura en una alarma sobre el documento.
    """
    if not contenido:
        return resumir_firmas([])

    import io

    import pdfplumber

    todas: list[bool] = []
    texto: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(contenido)) as pdf:
            for pagina in pdf.pages:
                texto.append(pagina.extract_text() or "")
                lineas = [
                    {"x0": w["x0"], "x1": w["x1"], "top": w["top"]}
                    for w in pagina.extract_words()
                    if _LINEA_FIRMA_RE.match(w["text"])
                ]
                if not lineas:
                    continue  # sigue acumulando texto de las demás páginas
                imagenes = [
                    {"x0": im["x0"], "x1": im["x1"], "top": im["top"]}
                    for im in pagina.images
                ]
                todas.extend(lineas_firmadas(lineas, imagenes))
    except Exception as exc:
        logger.warning("Firmas: no se pudo leer el PDF (%s): %s", type(exc).__name__, exc)
        return resumir_firmas([])

    return {**resumir_firmas(todas), "tipo": tipo_por_contenido(" ".join(texto))}


# Regla dada por el usuario (2026-08-20): un mandato de INGRESOS trae esta
# leyenda al pie del cuadro de valores; uno de costos no. Los de costos no
# mencionan la palabra "costo" en ninguna parte del texto -- verificado sobre
# los lotes reales -- así que el discriminador tiene que ser la leyenda.
#
# Se busca un fragmento corto y sin tildes ni saltos de línea: el texto real
# viene partido en dos renglones ("... DEL ARTÍCULO\n476 DEL E.T.") y con
# acentos que dependen de cómo extraiga pdfplumber.
_LEYENDA_INGRESOS = "ingresos de venta de energia son excluidos de iva"


def tipo_por_contenido(texto: str | None) -> str:
    """'ingreso' si el texto trae la leyenda de exclusión de IVA; si no 'costo'.

    Hace falta porque el NOMBRE no alcanza: un lote de autoconsumo puede traer
    ingresos y costos con la misma convención de archivo
    ("CMU####-Mandato-{Empresa}.pdf"), y el tipo es parte de la identidad única
    del mandato. Clasificar los dos igual los haría chocar en la misma fila.
    """
    from apps.mandatos.services.email_parser import _normaliza

    # Sin saltos de línea: la leyenda viene partida en dos renglones.
    return ("ingreso" if _LEYENDA_INGRESOS in re.sub(r"\s+", " ", _normaliza(texto))
            else "costo")
