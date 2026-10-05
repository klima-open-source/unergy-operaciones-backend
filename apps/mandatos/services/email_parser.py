"""Parsing puro de correos de mandatos: HTML→texto, clasificación y extracción.

Sin red, sin base de datos, sin estado. Toda la fragilidad del sistema vive
acá, por eso se prueba contra los correos reales (tests/fixtures_mandatos_correos.py).
Si Vanessa cambia su redacción, el fix es agregar el correo nuevo como fixture
y ajustar estas funciones -- nada más del sistema debería moverse.
"""
from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser

from apps.mandatos.services.reglas import CMU_RE

# Etiquetas que implican salto de línea. Nota: etiquetas autocerradas como
# <br/> disparan tanto el start-tag como el end-tag, así que aportan DOS saltos
# de línea -- inofensivo porque las líneas vacías se filtran al final, pero es
# una trampa latente si alguien toca esta lógica.
_BLOQUE = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "ul", "ol"}
# Celdas de tabla: no rompen la línea (para no partir las filas de las tablas
# de comparación que Vanessa embebe), pero SÍ necesitan un separador entre
# ellas -- sin esto "5,703,802" y "5,475,170.65" quedarían pegados.
_CELDA = {"td", "th"}
_IGNORAR = {"script", "style"}


class _ExtractorTexto(HTMLParser):
    # Por defecto HTMLParser trata <script>/<style> como CDATA: si el cierre
    # nunca llega, todo lo que sigue (incluidas etiquetas reales, como un <p>
    # con un CMU) queda embebido como texto crudo y handle_starttag jamás se
    # entera de que hay un <p> ahí -- nuestra lógica de "recuperación" de más
    # abajo no podría dispararse nunca. Desactivamos ese modo especial para
    # que las etiquetas dentro de un script/style mal cerrado se sigan
    # parseando como etiquetas normales.
    #
    # Contrapartida aceptada: un cuerpo de script/style que contenga algo con
    # forma de par de etiquetas balanceado (p. ej. `var t = "<p>x</p>"`) hace
    # que ese texto interno se emita como si fuera del documento. Se acepta
    # porque este parser solo ve correo interno escrito a mano en Gmail/Outlook,
    # nunca HTML de terceros: nadie redacta <script> a mano, y el CSS de un
    # <style> no lleva "<" literal (es CSS inválido). Si algún día esto pasara a
    # leer correo de remitentes arbitrarios, hay que revisar esta decisión.
    CDATA_CONTENT_ELEMENTS: tuple[str, ...] = ()

    def __init__(self) -> None:
        super().__init__()
        self.partes: list[str] = []
        self._saltando = False

    def handle_starttag(self, tag: str, attrs) -> None:
        # Un <script>/<style> sin su cierre correspondiente (webmail truncado
        # o mal formado) no debe silenciar el resto del documento para siempre:
        # si mientras "saltamos" aparece una etiqueta de bloque, asumimos que
        # el tag ignorado nunca se cerró y retomamos el procesamiento normal.
        #
        # La recuperación es de mejor esfuerzo, no total: si dentro de ese
        # script/style sin cerrar aparece además un token con forma de etiqueta
        # sin terminar (`<div){x=1}` sin ">"), el tokenizador se lo traga junto
        # con lo que sigue y ese contenido se pierde. Hace falta que se den las
        # dos rarezas a la vez, así que se documenta en vez de arreglarse: el
        # arreglo exigiría reimplementar recuperación de tokens malformados,
        # desproporcionado para un módulo que lee un solo remitente conocido.
        if self._saltando and tag in _BLOQUE:
            self._saltando = False
        if tag in _IGNORAR:
            self._saltando = True
        elif tag in _BLOQUE:
            self.partes.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _IGNORAR:
            self._saltando = False
        elif tag in _BLOQUE:
            self.partes.append("\n")
        elif tag in _CELDA:
            self.partes.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._saltando:
            self.partes.append(data)


def html_a_texto(html: str | None) -> str:
    """HTML de correo → texto plano, una línea por bloque, sin líneas vacías.

    HTMLParser desescapa las entidades solo (convert_charrefs por defecto).
    """
    if not html:
        return ""
    extractor = _ExtractorTexto()
    extractor.feed(html)
    extractor.close()
    crudo = "".join(extractor.partes)
    lineas = [re.sub(r"[ \t\xa0]+", " ", l).strip() for l in crudo.split("\n")]
    return "\n".join(l for l in lineas if l)


def _normaliza(texto: str | None) -> str:
    """Minúsculas sin tildes, para comparar frases con redacción variable."""
    nfkd = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


CLASIF_MOLDE_SIMPLE = "molde_simple"
CLASIF_SEGUIMIENTO = "seguimiento"
CLASIF_DESCONOCIDO = "desconocido"

# Señales de que el correo declara ALGÚN CMU como resuelto. Solo estas bloquean
# la interpretación, porque solo estas crean ambigüedad sobre a cuáles aplica la
# observación. Correo real del 2026-08-10 5:50 p.m.: "Agradezco su respuesta y
# los ajustes realizados para el mandato CMU1255. Sin embargo, para los mandatos
# CMU1266... siguen siendo las mismas" -- ahí CMU1255 quedó bien y los otros no.
#
# NO van acá los marcadores de simple respuesta ("en respuesta a", "su
# respuesta"): decir que hay hilo no dice que algo se haya resuelto. Un correo
# que responde afirmando un problema uniforme -- "los mandatos CMU0746...
# CMU0749 aún presentan diferencias" -- es seguro de interpretar.
_SENALES_SEGUIMIENTO = (
    "agradezco",
    "sin embargo",
    "ajustes realizados",
    "ya se encuentra",
    "quedo corregido",
    "fueron corregid",
)

# Frases que introducen o afirman observaciones. Ampliadas con la redacción real
# vista en la bitácora de la primera corrida.
_SENALES_MOLDE = (
    "siguientes observaciones",
    "siguientes diferencias",
    "siguientes novedades",
    "siguientes inconsistencias",
    "diferencias identificadas",
    "presentan diferencias",
    "no se evidencia",
)


def clasificar_correo(asunto: str | None, cuerpo: str | None) -> str:
    """molde_simple | seguimiento | desconocido.

    Seguimiento se evalúa PRIMERO y gana ante señales mezcladas: interpretar de
    menos deja trabajo manual, interpretar de más corrompe estados en silencio.

    A propósito, esta función mira el cuerpo COMPLETO (incluida cualquier cita
    del hilo), a diferencia de extraer_observaciones() que trabaja sobre el
    cuerpo recortado -- ver _sin_cita(). Un "agradezco" enterrado en una cita
    empujando el correo a seguimiento es un resultado seguro: en el peor caso
    exige revisión manual. Clasificar de más (leer de más) no corrompe nada acá;
    extraer de más sí. Por eso una función es permisiva y la otra estricta.

    `asunto` se conserva en la firma -- otras partes del código lo pasan y
    puede volver a hacer falta -- pero ya no se lee: el prefijo "Re:" dejó de
    ser señal (ver más abajo).
    """
    # El prefijo "Re:" ya NO se usa como señal, así que `asunto` no se
    # normaliza ni se consulta acá: todos los correos reales de la revisoría
    # son respuestas dentro de un hilo, y esa regla sola clasificó 94 de 94
    # como seguimiento, dejando la Fuente 1 sin ejecutarse nunca. Lo que
    # importa no es si es respuesta, sino si declara algún CMU resuelto.
    c = _normaliza(cuerpo)
    if any(s in c for s in _SENALES_SEGUIMIENTO):
        return CLASIF_SEGUIMIENTO
    if any(s in c for s in _SENALES_MOLDE):
        return CLASIF_MOLDE_SIMPLE
    return CLASIF_DESCONOCIDO


# Líneas desde las que el cuerpo deja de tener contenido útil. Sin este corte,
# un CMU citado en la firma o en el hilo previo se leería como observación.
_INICIO_FIRMA = (
    "cordialmente",
    "quedo atenta",
    "quedo atento",
    "saludos",
    "atentamente",
)


# Encabezados que marcan el arranque del historial citado de un hilo. Cortar
# acá es crítico: un CMU citado del correo anterior puede figurar como YA
# RESUELTO (ver REVISORIA_SEGUIMIENTO en los fixtures) y si extraer_observaciones
# sigue leyendo después del corte, ese CMU resuelto se guarda como corrección
# nueva -- el mismo peligro que motiva clasificar_correo, colándose por otra vía.
_INICIO_CITA_GMAIL_RE = re.compile(r"^el .*\d.* escribio:$")
_INICIO_CITA_OUTLOOK_RE = re.compile(r"^on .*\d.* wrote:$")
_SEPARADORES_CITA = ("-----mensaje original-----", "-----original message-----")


def _sin_cita(cuerpo: str) -> str:
    """Todo el texto ANTES de que arranque el historial citado del hilo.

    Corta en la primera línea que sea, en cualquier variante:
    - una cita de respuesta línea a línea (prefijo '>')
    - un encabezado de Gmail ("El vie, 10 ago 2026 ... escribió:")
    - un encabezado de Outlook en inglés ("On ... wrote:")
    - un separador de reenvío de Outlook ("-----Mensaje original-----")
    - el bloque de encabezados reenviados de Outlook (De:/Para:/Asunto: en
      las líneas siguientes a una que empieza con "de:"/"from:")
    """
    lineas = cuerpo.split("\n")
    for i, linea in enumerate(lineas):
        n = _normaliza(linea)
        if linea.lstrip().startswith(">"):
            return "\n".join(lineas[:i])
        if _INICIO_CITA_GMAIL_RE.match(n) or _INICIO_CITA_OUTLOOK_RE.match(n):
            return "\n".join(lineas[:i])
        if n in _SEPARADORES_CITA:
            return "\n".join(lineas[:i])
        if n.startswith(("de:", "from:")):
            siguientes = " ".join(_normaliza(l) for l in lineas[i + 1 : i + 5])
            tiene_para = "para:" in siguientes or "to:" in siguientes
            tiene_asunto = "asunto:" in siguientes or "subject:" in siguientes
            if tiene_para and tiene_asunto:
                return "\n".join(lineas[:i])
    return cuerpo


def parece_nombre_de_mandato(nombre: str | None) -> bool:
    """Si el archivo pretende ser un mandato, aunque no se pueda interpretar.

    Sirve para separar dos cosas que no valen lo mismo: "esto parece un mandato
    pero no pude leer su nombre" (hay que revisarlo) y "esto es una factura
    adjunta a un correo de Jessica" (no tiene nada que ver). Sin la distinción,
    cualquier PDF suelto levantaba la bandera de revisión y el panel se llenaba
    de facturas y comprobantes de pago -- 136 de 385 correos en la corrida del
    2026-08-20. Un panel que grita por todo no lo mira nadie.
    """
    n = _normaliza(nombre)
    return "mandato" in n or "certificado" in n or bool(CMU_RE.search(n.upper()))


# Frases con las que un correo saliente declara que las correcciones YA se
# hicieron. Confirmado con el usuario (2026-08-20): tras recibir las
# observaciones, Adhara o Vanessa Aguirre responden compartiéndolas corregidas,
# y ese es el paso que faltaba para salir de `con_comentarios`.
#
# Cada frase exige un verbo de ENTREGA ("comparto", "envío", "adjunto") junto a
# la corrección. Buscar solo "correcciones" marcaría también los correos que las
# PIDEN -- "por favor realizar las correcciones" -- que son lo contrario.
# Se exige un verbo de ENTREGA y una palabra de corrección. Buscar solo la
# corrección marcaría también los correos que la PIDEN ("por favor realizar las
# correcciones"), que son lo contrario.
_VERBOS_ENTREGA = ("comparto", "envio", "adjunto", "remito", "reenvio")

# Correos reales de Adhara (2026-08-20): "Comparto los mandatos faltantes y
# CORREGIDOS", "los mandatos y los apuntes contables ya CORREGIDOS". Usa el
# adjetivo, no el sustantivo -- la primera versión buscaba "correcciones" y no
# encontró ninguno en 90 días de correo.
#
# "corregid" no captura "corregir", y eso es a propósito: "por favor corregir"
# es una petición, no una entrega.
_PALABRAS_CORRECCION = ("corregid", "correccion")


def es_correo_de_correcciones(cuerpo: str | None) -> bool:
    """Si el correo declara que las correcciones ya se hicieron y se comparten.

    Solo tiene sentido sobre correos SALIENTES hacia la revisoría. Un correo de
    la revisoría que reporta diferencias no es una corrección -- es lo contrario.
    """
    n = _normaliza(cuerpo)
    return (any(v in n for v in _VERBOS_ENTREGA)
            and any(p in n for p in _PALABRAS_CORRECCION))


# Lenguaje de conformidad: la línea confirma que ese mandato quedó bien, no lo
# observa. Regla de negocio: un mandato firmado no lleva correcciones, así que
# una línea así NUNCA debe producir un con_comentarios.
#
# Es una lista de bloqueo (no de permiso) a propósito: las formas de decir "este
# quedó bien" son pocas y formulaicas, mientras que las de reportar un problema
# son infinitas. Una lista de permiso se comería hallazgos reales.
_SENALES_CONFORME = (
    "debidamente firmad",
    "se encuentran firmad",
    "se encuentra firmad",
    "sin novedad",
    "sin observacion",
    "sin ninguna observacion",
    "conforme",
    "aprobad",
)

# Cancelan la conformidad: "el mandato NO está firmado" es un hallazgo, no una
# confirmación. Ante la duda se extrae — perder una observación real es peor
# que dejar pasar un CMU conforme.
_NIEGAN_CONFORMIDAD = (
    "no ",
    "sin firma",
    "falta",
    "pendiente",
    "pero ",
    "salvo",
)


def _linea_confirma_conformidad(linea: str) -> bool:
    n = _normaliza(linea)
    if not any(s in n for s in _SENALES_CONFORME):
        return False
    return not any(s in n for s in _NIEGAN_CONFORMIDAD)


def extraer_observaciones(cuerpo: str | None) -> list[dict]:
    """[{'cmu': 'CMU1255', 'observacion': '...'}] en orden de aparición.

    Una línea puede traer varios CMU compartiendo una misma observación
    (correo real: "Certificados CMU1266,CMU1269,CMU1270 y CMU1271 no se
    evidencia contabilización..."). La observación es lo que sigue al ÚLTIMO
    CMU de la línea. Un CMU repetido conserva su primera observación.

    Descarta las líneas que confirman conformidad ("los certificados CMU1160,
    CMU1161 se encuentran debidamente firmados"): un correo mixto trae la
    observación real y, aparte, el visto bueno de los demás. Ver
    _linea_confirma_conformidad().

    Solo debe llamarse con cuerpos clasificados CLASIF_MOLDE_SIMPLE. Trabaja
    sobre el cuerpo recortado por _sin_cita() -- ver el comentario ahí y el
    de clasificar_correo() sobre por qué esta función es la estricta.

    Límite conocido y aceptado: si un correo en texto plano hace wrap de una
    observación larga en dos líneas, la línea de continuación no tiene CMU y
    se descarta en silencio, truncando la observación guardada. No se
    implementa unión de líneas de continuación porque el riesgo es bajo: el
    camino real es HTML (Gmail/Outlook componen en HTML), y html_a_texto()
    ya entrega una línea por bloque, sin wraps de texto plano.
    """
    resultado: list[dict] = []
    vistos: set[str] = set()
    for linea in _sin_cita(cuerpo or "").split("\n"):
        if _normaliza(linea).startswith(_INICIO_FIRMA):
            break
        cmus = CMU_RE.findall(linea)
        if not cmus:
            continue
        if _linea_confirma_conformidad(linea):
            continue
        corte = linea.rfind(cmus[-1]) + len(cmus[-1])
        observacion = linea[corte:].strip().strip(".,:;-–—").strip()
        for cmu in cmus:
            if cmu in vistos:
                continue
            vistos.add(cmu)
            resultado.append({"cmu": cmu, "observacion": observacion})
    return resultado


# Fuente 3: convención verificada en los correos de Jessica
# ("CMU1135-Mandato-Costos-{Proyecto}-{Inversionista}.pdf"). Anclada al inicio
# para no confundir un CMU citado en otra parte del nombre.
_CMU_INICIO_RE = re.compile(r"^(CMU\d+)", re.IGNORECASE)


def cmu_al_inicio_de_nombre(nombre: str | None) -> str | None:
    """'CMU1135-Mandato-Costos-....pdf' → 'CMU1135'. None si no arranca con CMU.

    Para Fuente 2 (revisoría), cuya convención de nombres NO está verificada,
    usar extraer_cmu_de_nombre() de mandatos_service, que busca en cualquier parte.
    """
    m = _CMU_INICIO_RE.match((nombre or "").strip())
    return m.group(1).upper() if m else None


def solo_pdfs(nombres: list[str]) -> list[str]:
    """Los nombres que terminan en .pdf, conservando el orden."""
    return [n for n in nombres if (n or "").lower().endswith(".pdf")]


# El mandante (tercero) de la identidad de Finanzas. En los correos de Jessica
# aparece como "17844 - P.A SOL DE LA SIERRA": un código numérico seguido del
# nombre en mayúsculas. El CÓDIGO es lo estable -- los nombres se escriben con
# y sin tilde, con y sin puntos ("P.A" vs "P.A."), así que cruzar por nombre es
# frágil y cruzar por código no.
#
# El nombre corre hasta que aparece minúscula ("del mes de..."), coma o fin de
# línea. Exigir el código evita morder el saludo, que nombra a la fiduciaria
# ("PATRIMONIOS AUTONOMOS FIDUCIARIA BANCOLOMBIA") sin ser la identidad.
_PA_RE = re.compile(
    r"(\d{4,6})\s*-\s*(P\.?\s?A\.?\s+[A-ZÁÉÍÓÚÑ0-9][A-ZÁÉÍÓÚÑ0-9\s\.\-]*?)"
    r"(?=\s+(?:del|de|para)\s|[,\n]|$)",
    re.UNICODE,
)

# Patrón laxo, solo para CONTAR menciones. _PA_RE es estricto en dónde termina
# el nombre, así que un P.A. seguido de texto inesperado ("... SIERRA y 18254 ...")
# simplemente no matchea -- y entonces contar coincidencias de _PA_RE haría creer
# que el cuerpo menciona un solo patrimonio cuando menciona dos. Para detectar
# ambigüedad hay que contar candidatos con la manga ancha y extraer con la angosta.
_PA_CANDIDATO_RE = re.compile(r"(\d{4,6})\s*-\s*P\.?\s?A\.?\s", re.UNICODE)


def extraer_pa_del_cuerpo(cuerpo: str | None) -> dict | None:
    """{'codigo': '17844', 'nombre': 'P.A SOL DE LA SIERRA'} o None.

    Solo reconoce el patrón `código - NOMBRE`. Si el correo nombra un patrimonio
    sin código, devuelve None: preferimos no identificar a identificar mal, porque
    el tercero es parte de la identidad y equivocarlo crea una fila fantasma.

    Por la misma razón, un cuerpo que menciona DOS códigos distintos devuelve None
    en vez de elegir uno. Los correos reales nombran un solo P.A., pero sin esta
    guarda cuál gana lo decidía un accidente del regex (dónde se corta cada
    captura), no una regla -- y "elegir por accidente" es exactamente identificar
    mal. Si esto empieza a aparecer, hay que ver correos reales y decidir la regla.
    """
    texto = cuerpo or ""
    if len(set(_PA_CANDIDATO_RE.findall(texto))) > 1:
        return None
    m = _PA_RE.search(texto)
    if not m:
        return None
    return {"codigo": m.group(1),
            "nombre": re.sub(r"\s+", " ", m.group(2)).strip()}


def mandatos_enviados_en_correo(nombres_adjuntos: list[str]) -> list[dict]:
    """Los mandatos que salieron en un correo, uno por adjunto reconocible.

    [{'cmu': 'CMU1140', 'proyecto': 'Minigranja Solar Merengue',
      'inversionista': '', 'tipo': 'costo'}, ...]

    Solo cuenta adjuntos que cumplen la convención de nombre. Un PDF suelto que
    no la cumple NO entra: esta lista alimenta el conteo de la reconciliación, y
    un adjunto de más ahí inventa un mandato que nadie envió y que después
    aparece eternamente como "falta por volver".
    """
    from apps.mandatos.services.reglas import parsear_nombre_zip

    salida: list[dict] = []
    for nombre in solo_pdfs(nombres_adjuntos):
        parsed = parsear_nombre_zip(nombre)
        if not parsed:
            continue
        salida.append({
            "cmu": parsed["cmu"],
            "proyecto": parsed["proyecto"],
            "inversionista": parsed["inversionista"],
            # La convención solo distingue Costos; los de ingresos usan otra
            # palabra en el nombre y hoy no hay muestra real. Se marca 'costo'
            # porque es lo único verificado -- cuando aparezca un adjunto de
            # ingresos, agregarlo como fixture y ajustar acá.
            "tipo": "costo",
        })
    return salida
