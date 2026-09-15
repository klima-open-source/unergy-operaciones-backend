"""Comparación difusa de nombres — utilidad compartida entre dominios.

`apps/comun/` es un paquete Python, NO una app de Django: no tiene modelos y no
va en INSTALLED_APPS. Existe porque este algoritmo lo usan tres dominios
(operadores de red, fronteras y proyectos) para avisar de posibles duplicados,
y una función pura que comparten tres dominios no pertenece a ninguno.

Algoritmo puro de comparación de nombres de proyectos/fronteras -- sin
dependencias de base de datos ni de la app, para que lo puedan importar tanto
código que corre dentro del backend (con sesión de BD) como scripts externos
que solo reciben listas de nombres por HTTP.

Es la misma cadena que se usó para reconciliar fronteras.proyecto_id contra
producción (2026-07-02, scripts/etl_fronteras_proyectos.py, ya retirado):
normaliza, quita prefijos de ruido ("Minigranja Solar", "GD", "MGS", "Consumo
Aux", etc.) que antes causaban falsos matches entre proyectos distintos,
compara solapamiento de tokens + similitud de texto, y no acepta un resultado
si el segundo mejor candidato queda casi tan bien como el primero (mejor no
adivinar que adivinar mal).

Usado por:
  - app/utils/proyecto_matching.py  (find_proyecto_by_name, con sesión de BD)
"""
import re
import unicodedata
from difflib import SequenceMatcher

UMBRAL_ACEPTAR = 0.55
MARGEN_AMBIGUO = 0.05

# Cuánto tiene que parecerse el TEXTO cuando los dos nombres no comparten NI UNA
# palabra significativa.
#
# La similitud de texto está para tolerar erratas --«Caracoli» contra
# «Caracolli»--, no para emparentar nombres distintos. Pero sin este piso podía
# sostener un match ella sola: «GD Caracolí 2» contra «La Catedral» puntuaba
# 0,556 --por encima del umbral-- sin una sola palabra en común, solo porque las
# letras se parecen (caso real, 2026-09-15).
#
# 0,80 deja pasar una errata de verdad (una letra de más en una palabra da 0,9 o
# más) y corta el parecido accidental, que ronda 0,5.
UMBRAL_SIN_TOKENS_COMUNES = 0.80

_STOPWORDS = {
    "de", "del", "la", "el", "los", "las", "y", "en",
    "minigranja", "minigranjas", "mgs", "mgr", "gd", "planta", "granja",
    "solar", "sol", "cielo", "frontera", "proyecto",
    "consumo", "auxiliar", "aux", "propio", "serv", "ser", "generacion",
    # Casi todo municipio colombiano en el portafolio empieza por «San»/«Santa»:
    # compartirlo no dice nada, y sin sacarlo «GD San Pelayo» y «GD San Marcos»
    # -- dos pueblos distintos -- puntuaban 0,600 y se avisaban como parecidos.
    # Mismo criterio que ya usa el pipeline del Reporte de Energía para mapear
    # Quoia contra Solenium.
    "san", "santa",
}

# El número de la minigranja es un IDENTIFICADOR, no una palabra más.
# Misma convención que `_mgs_number` en app/services/mgs/gaia_client.py: exige
# el prefijo explícito, así que no confunde un año ni un número suelto de una
# razón social.
_NUMERO_MGS = re.compile(r"\b(?:minigranja|minigranjas|mgs|mgr)\s+0*(\d+)\b")


# Sufijos societarios (razón social de empresa, no de proyecto/frontera) --
# se quitan ANTES de tirar la puntuación, para que "S.A.S." se reconozca como
# una sola unidad y no como las letras sueltas "s"/"a"/"s" tras normalizar.
_SUFIJOS_SOCIETARIOS = re.compile(
    r"\b(s[.\s]?a[.\s]?s\.?|e[.\s]?s[.\s]?p\.?|s[.\s]?a\.?|ltda\.?|bic)\b", re.IGNORECASE
)


def normalizar(texto: str) -> str:
    """Quita tildes, pone minúsculas, sufijos societarios (S.A.S./LTDA/E.S.P.)
    y elimina caracteres no alfanuméricos."""
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", texto)
    ascii_str = nfkd.encode("ascii", "ignore").decode("ascii").lower()
    ascii_str = _SUFIJOS_SOCIETARIOS.sub(" ", ascii_str)
    return re.sub(r"[^a-z0-9\s]", " ", ascii_str).strip()


def core_tokens(nombre: str) -> set[str]:
    """Tokens significativos de un nombre (sin stopwords de ruido tipo
    'Minigranja Solar' / 'GD' / 'Consumo Aux')."""
    return {t for t in normalizar(nombre).split() if t and t not in _STOPWORDS}


def numero_mgs(nombre: str) -> int | None:
    """El número de la minigranja, o None si el nombre no lo trae.

    «Minigranja 0091 - San Luis de Sincé» → 91. Se normaliza primero para que
    funcione con tildes y con cualquier puntuación intermedia.
    """
    if not nombre:
        return None
    encontrado = _NUMERO_MGS.search(normalizar(nombre))
    return int(encontrado.group(1)) if encontrado else None


def score_nombre(nombre_a: str, nombres_b: list[str]) -> float:
    """Mejor score entre nombre_a y cualquiera de nombres_b: combina solapamiento
    de tokens (orden-independiente) con similitud de texto (tolera typos).

    Si los dos nombres traen número de minigranja, ese número decide y no se
    negocia: es el identificador del proyecto, no una palabra. Números
    distintos ⇒ son minigranjas distintas por más que compartan el municipio;
    número igual ⇒ es la misma, por más que el nombre esté escrito de otra
    forma. Sin esto, «Minigranja 0091 - San Luis de Sincé» contra «Minigranja
    0088 - San Luis» puntuaba 0,688 y se avisaba como duplicado.
    """
    tokens_a = core_tokens(nombre_a)
    numero_a = numero_mgs(nombre_a)
    if not tokens_a:
        return 0.0
    mejor = 0.0
    for nb in nombres_b:
        if not nb:
            continue
        numero_b = numero_mgs(nb)
        if numero_a is not None and numero_b is not None:
            if numero_a != numero_b:
                continue          # minigranjas distintas: no compiten
            mejor = 1.0           # la misma, sin importar cómo esté escrita
            continue
        tokens_b = core_tokens(nb)
        if not tokens_b:
            continue
        inter = tokens_a & tokens_b
        jaccard = len(inter) / len(tokens_a | tokens_b) if (tokens_a | tokens_b) else 0.0
        overlap = len(inter) / min(len(tokens_a), len(tokens_b))
        ratio = SequenceMatcher(
            None, " ".join(sorted(tokens_a)), " ".join(sorted(tokens_b))
        ).ratio()
        # Sin una sola palabra en común, el parecido de texto tiene que ser
        # MUCHO mayor para contar: ahí ya no está tolerando una errata, está
        # emparentando dos nombres distintos. Ver UMBRAL_SIN_TOKENS_COMUNES.
        if not inter and ratio < UMBRAL_SIN_TOKENS_COMUNES:
            ratio = 0.0
        mejor = max(mejor, jaccard, overlap * 0.85, ratio)
    return round(mejor, 3)


# Indicadores de persona jurídica en la razón social -- para sugerir
# tipo_persona al crear un cliente. Aparte de _SUFIJOS_SOCIETARIOS (que se
# QUITAN del nombre para comparar) porque aquí es al revés: su PRESENCIA es
# la señal. Incluye fiduciaria/patrimonio autónomo/fideicomiso, que no son
# sufijos societarios pero tampoco son personas naturales (ej. "PATRIMONIOS
# AUTONOMOS FIDUCIARIA BANCOLOMBIA S A SOCIEDAD FIDUCIARIA").
_INDICADORES_PERSONA_JURIDICA = re.compile(
    r"\b(s[.\s]?a[.\s]?s\.?|e[.\s]?s[.\s]?p\.?|s[.\s]?a\.?|ltda\.?|bic|"
    r"fiduciaria|patrimonio\s+autonomo|fideicomiso)\b",
    re.IGNORECASE,
)


def parece_persona_juridica(nombre: str) -> bool:
    """True si la razón social trae un indicador reconocible de persona
    jurídica. Solo es señal POSITIVA -- su ausencia NO implica persona
    natural (no hay suficientes clientes reales marcados 'natural' en la
    plataforma para validar ese lado de la regla), así que no se usa para
    sugerir 'natural', solo para sugerir 'juridica' cuando aplica."""
    if not nombre:
        return False
    nfkd = unicodedata.normalize("NFKD", nombre)
    ascii_str = nfkd.encode("ascii", "ignore").decode("ascii").lower()
    return bool(_INDICADORES_PERSONA_JURIDICA.search(ascii_str))


def mejor_candidato(nombre_objetivo: str, candidatos: list[tuple]) -> tuple:
    """Elige el mejor candidato para nombre_objetivo.

    candidatos: lista de (id_o_objeto, [nombres...]) -- el segundo elemento de
    cada tupla es la lista de nombres alternativos de ese candidato.

    Devuelve (id_o_objeto, score) del ganador, o (None, score) si ninguno supera
    el umbral, o si los dos mejores quedan demasiado cerca entre sí (ambiguo --
    mejor no adivinar)."""
    puntajes = sorted(
        ((item, score_nombre(nombre_objetivo, nombres)) for item, nombres in candidatos),
        key=lambda x: -x[1],
    )
    if not puntajes:
        return None, 0.0
    mejor_item, mejor_score = puntajes[0]
    segundo_score = puntajes[1][1] if len(puntajes) > 1 else 0.0

    if mejor_score < UMBRAL_ACEPTAR:
        return None, mejor_score
    if (mejor_score - segundo_score) < MARGEN_AMBIGUO and segundo_score >= UMBRAL_ACEPTAR:
        return None, mejor_score  # ambiguo entre 2+ candidatos

    return mejor_item, mejor_score
