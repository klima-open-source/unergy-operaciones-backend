"""Tests del parser de correos de mandatos -- funciones puras, sin red ni BD."""
from apps.mandatos.services.email_parser import (
    CLASIF_DESCONOCIDO, CLASIF_MOLDE_SIMPLE, CLASIF_SEGUIMIENTO,
    clasificar_correo, cmu_al_inicio_de_nombre, extraer_observaciones,
    es_correo_de_correcciones, extraer_pa_del_cuerpo, html_a_texto,
    mandatos_enviados_en_correo, solo_pdfs,
)
from tests.fixtures_mandatos_correos import (
    ADJUNTOS_REALES_DRIVE, ENVIO_INVERSIONISTA, ENVIO_INVERSIONISTA_ADJUNTOS,
    LIQUIDACION_PRELIMINAR, LIQUIDACION_PRELIMINAR_ADJUNTOS,
    REVISORIA_HTML, REVISORIA_MIXTO, REVISORIA_OBSERVACIONES,
    REVISORIA_RESPUESTA_UNIFORME, REVISORIA_SEGUIMIENTO,
)


def test_html_a_texto_desescapa_entidades():
    texto = html_a_texto("<p>informaci&oacute;n&nbsp;compartida</p>")
    assert texto == "información compartida"


def test_html_a_texto_separa_bloques_en_lineas():
    texto = html_a_texto("<p>uno</p><p>dos</p>")
    assert texto.split("\n") == ["uno", "dos"]


def test_html_a_texto_ignora_script_y_style():
    texto = html_a_texto("<p>visible</p><style>.x{color:red}</style><script>var a=1</script>")
    assert texto == "visible"


def test_html_a_texto_conserva_los_cmu_de_cada_linea():
    texto = html_a_texto(REVISORIA_HTML)
    lineas_con_cmu = [l for l in texto.split("\n") if "CMU" in l]
    assert len(lineas_con_cmu) == 3
    assert "CMU1266,CMU1269,CMU1270 y CMU1271" in lineas_con_cmu[1]


def test_html_a_texto_vacio():
    assert html_a_texto("") == ""
    assert html_a_texto(None) == ""


def test_html_a_texto_separa_celdas_de_tabla_con_espacio():
    texto = html_a_texto(REVISORIA_HTML)
    assert "Certificado Contabilidad" in texto
    assert "5,703,802 5,475,170.65" in texto


def test_html_a_texto_recupera_texto_tras_script_sin_cerrar():
    texto = html_a_texto("<p>antes</p><script>var a = 1;<p>CMU1266 dato importante</p>")
    assert "CMU1266" in texto


def test_html_a_texto_no_revienta_con_etiquetas_sin_cerrar():
    texto = html_a_texto("<div><p>CMU1000 texto")
    assert "CMU1000" in texto


def test_html_a_texto_limite_conocido_script_sin_cerrar_con_token_malformado():
    """Fija un límite ACEPTADO, no un comportamiento deseado.

    Si un <script> sin cerrar contiene además un token con forma de etiqueta
    sin terminar (`<div){...}` sin ">"), el tokenizador se traga el resto y ese
    contenido se pierde. Requiere dos rarezas simultáneas y arreglarlo exigiría
    reimplementar recuperación de tokens malformados (ver comentario en
    handle_starttag). Este test existe para que un refactor futuro que cambie
    esto -- para bien o para mal -- no pase inadvertido.
    """
    texto = html_a_texto("<p>antes</p><script>if(a<div){secreto=3}<p>CMU3333</p>")
    assert texto == "antes"


# ── clasificar_correo ─────────────────────────────────────────────────────────

def test_clasificar_observaciones_nuevas_es_molde_simple():
    assert clasificar_correo("Mandatos de costos julio", REVISORIA_OBSERVACIONES) == CLASIF_MOLDE_SIMPLE


def test_clasificar_correo_mixto_es_molde_simple():
    assert clasificar_correo("Certificados Sol de la Sierra", REVISORIA_MIXTO) == CLASIF_MOLDE_SIMPLE


def test_clasificar_seguimiento_no_se_interpreta():
    """El correo donde CMU1255 quedó resuelto. Si esto se rompe, el sistema
    empieza a marcar mandatos resueltos como con_correcciones."""
    assert clasificar_correo("Mandatos de costos julio", REVISORIA_SEGUIMIENTO) == CLASIF_SEGUIMIENTO


def test_seguimiento_gana_sobre_molde_simple():
    """Ante señales de ambos, gana seguimiento -- falla hacia el lado seguro."""
    cuerpo = "Agradezco su respuesta. Encuentro las siguientes observaciones: CMU1000 mal"
    assert clasificar_correo("Certificados", cuerpo) == CLASIF_SEGUIMIENTO


def test_clasificar_correo_sin_senales_es_desconocido():
    assert clasificar_correo("Hola", "Buenas tardes, quedo atenta.") == CLASIF_DESCONOCIDO


def test_una_respuesta_uniforme_si_se_interpreta():
    """Correo real del 1 jun. Es una respuesta en hilo, pero los cuatro CMU
    tienen el mismo problema y ninguno está resuelto: no hay ambigüedad."""
    assert clasificar_correo(
        "Re: Revisión mandatos de costos - Junio",
        REVISORIA_RESPUESTA_UNIFORME) == CLASIF_MOLDE_SIMPLE


def test_el_asunto_re_por_si_solo_ya_no_bloquea():
    """TODOS los asuntos reales empiezan por Re:, porque son hilos. Esa regla
    sola clasificó 94 de 94 correos como seguimiento y dejó la Fuente 1 muerta."""
    assert clasificar_correo(
        "Re: Revisión mandatos de costos - Julio",
        REVISORIA_OBSERVACIONES) == CLASIF_MOLDE_SIMPLE


def test_el_correo_con_un_cmu_resuelto_sigue_bloqueado():
    """La regresión que importa: si esto se rompe, CMU1255 vuelve a marcarse
    con correcciones siendo el único que quedó bien."""
    assert clasificar_correo(
        "Re: Revisión mandatos de costos - Julio",
        REVISORIA_SEGUIMIENTO) == CLASIF_SEGUIMIENTO


def test_extrae_los_cuatro_cmu_de_la_respuesta_uniforme():
    obs = extraer_observaciones(REVISORIA_RESPUESTA_UNIFORME)
    assert [o["cmu"] for o in obs] == ["CMU0746", "CMU0747", "CMU0748", "CMU0749"]


# ── extraer_observaciones ─────────────────────────────────────────────────────

def test_extraer_observaciones_correo_real():
    obs = extraer_observaciones(REVISORIA_OBSERVACIONES)
    assert [o["cmu"] for o in obs] == [
        "CMU1255", "CMU1266", "CMU1269", "CMU1270", "CMU1271", "CMU1284",
    ]


def test_varios_cmu_en_una_linea_comparten_observacion():
    obs = {o["cmu"]: o["observacion"] for o in extraer_observaciones(REVISORIA_OBSERVACIONES)}
    esperado = "no se evidencia contabilización del internet, el IVA y el arriendo"
    assert obs["CMU1266"] == esperado
    assert obs["CMU1271"] == esperado


def test_observacion_arranca_despues_del_ultimo_cmu():
    obs = {o["cmu"]: o["observacion"] for o in extraer_observaciones(REVISORIA_OBSERVACIONES)}
    assert obs["CMU1255"].startswith("el valor a pagar no coincide")
    assert obs["CMU1284"] == "no se evidencia contabilización"


def test_extraer_observaciones_correo_mixto():
    obs = extraer_observaciones(REVISORIA_MIXTO)
    assert [o["cmu"] for o in obs] == ["CMU1052", "CMU1122"]
    assert obs[0]["observacion"] == "No se evidencia contabilización del mantenimiento y el IVA de este"


def test_extraer_observaciones_corta_en_la_firma():
    cuerpo = "CMU1000 tiene novedad\nCordialmente\nCMU9999 esto es parte de la firma"
    assert [o["cmu"] for o in extraer_observaciones(cuerpo)] == ["CMU1000"]


def test_extraer_observaciones_sin_cmu():
    assert extraer_observaciones("Buenas tardes, quedo atenta.") == []


# ── adjuntos ──────────────────────────────────────────────────────────────────

def test_cmu_al_inicio_de_nombre_convencion_de_jessica():
    assert cmu_al_inicio_de_nombre(
        "CMU1135-Mandato-Costos-Minigranja Solar La Paz Levende.pdf") == "CMU1135"


def test_cmu_al_inicio_ignora_cmu_en_medio_del_nombre():
    """Ancla al inicio: un CMU suelto en medio del nombre no cuenta para Fuente 3."""
    assert cmu_al_inicio_de_nombre("REGISTRO MANDATOS CMU1135.xlsx") is None


def test_cmu_al_inicio_sin_match():
    assert cmu_al_inicio_de_nombre("REGISTRO MANDATOS.xlsx") is None
    assert cmu_al_inicio_de_nombre("") is None
    assert cmu_al_inicio_de_nombre(None) is None


def test_solo_pdfs_descarta_el_excel():
    assert solo_pdfs(ENVIO_INVERSIONISTA_ADJUNTOS) == [
        "CMU1135-Mandato-Costos-Minigranja Solar La Paz Levende.pdf",
    ]


def test_cmu_al_inicio_con_la_convencion_real_de_tres_partes():
    """Los nombres reales son `CMU####-Mandato-Costos-{Proyecto}.pdf`, sin
    sufijo de inversionista. El anclaje al inicio funciona igual."""
    assert [cmu_al_inicio_de_nombre(n) for n in ADJUNTOS_REALES_DRIVE] == [
        "CMU1135", "CMU1140", "CMU1147", "CMU1148",
    ]


def test_liquidacion_preliminar_no_aporta_cmu():
    """Caso negativo: correo de Jessica a inversionistas que menciona
    'certificados de mandato' pero no trae adjuntos de mandato."""
    pdfs = solo_pdfs(LIQUIDACION_PRELIMINAR_ADJUNTOS)
    assert [cmu_al_inicio_de_nombre(n) for n in pdfs] == []


# ── extraer_observaciones no debe leer el historial citado ────────────────────

def test_extraer_observaciones_no_lee_cita_estilo_gmail():
    """Repro del hallazgo: un CMU resuelto citado del hilo anterior no debe
    colarse como observación nueva."""
    cuerpo = (
        "Encuentro las siguientes observaciones:\n"
        "CMU1266 no se evidencia contabilizacion del arriendo\n"
        "\n"
        "El vie, 10 ago 2026 a las 14:25, Vanessa <...> escribio:\n"
        "> Certificado CMU1284 ya se encuentra resuelto"
    )
    cmus = [o["cmu"] for o in extraer_observaciones(cuerpo)]
    assert "CMU1266" in cmus
    assert "CMU1284" not in cmus


def test_extraer_observaciones_no_lee_lineas_con_prefijo_mayor_que():
    cuerpo = (
        "Encuentro las siguientes observaciones:\n"
        "CMU1000 tiene novedad nueva\n"
        "> CMU9999 esto viene del correo anterior\n"
    )
    assert [o["cmu"] for o in extraer_observaciones(cuerpo)] == ["CMU1000"]


def test_extraer_observaciones_no_lee_tras_separador_outlook():
    cuerpo = (
        "Encuentro las siguientes observaciones:\n"
        "CMU1000 tiene novedad nueva\n"
        "-----Mensaje original-----\n"
        "CMU9999 esto viene del correo anterior\n"
    )
    assert [o["cmu"] for o in extraer_observaciones(cuerpo)] == ["CMU1000"]


def test_extraer_observaciones_sin_cita_no_se_recorta():
    """Guarda de regresión más importante: un correo sin ninguna cita debe
    seguir devolviendo la lista completa de CMU."""
    obs = extraer_observaciones(REVISORIA_OBSERVACIONES)
    assert [o["cmu"] for o in obs] == [
        "CMU1255", "CMU1266", "CMU1269", "CMU1270", "CMU1271", "CMU1284",
    ]


# ── extraer_pa_del_cuerpo ─────────────────────────────────────────────────────

def test_extraer_pa_del_correo_real_de_jessica():
    assert extraer_pa_del_cuerpo(ENVIO_INVERSIONISTA) == {
        "codigo": "17844", "nombre": "P.A SOL DE LA SIERRA"}


def test_extraer_pa_tolera_puntuacion_y_tildes():
    cuerpo = "asociados al 18254 - P.A.  AUTOCONSUMO NESTLÉ del mes de julio ya"
    assert extraer_pa_del_cuerpo(cuerpo) == {
        "codigo": "18254", "nombre": "P.A. AUTOCONSUMO NESTLÉ"}


def test_extraer_pa_no_confunde_el_saludo_sin_codigo():
    """El saludo nombra la fiduciaria sin código -- no es la identidad."""
    cuerpo = "Cordial saludo equipo de PATRIMONIOS AUTONOMOS FIDUCIARIA BANCOLOMBIA S A"
    assert extraer_pa_del_cuerpo(cuerpo) is None


def test_extraer_pa_en_liquidacion_preliminar_es_none():
    assert extraer_pa_del_cuerpo(LIQUIDACION_PRELIMINAR) is None


def test_extraer_pa_vacio():
    assert extraer_pa_del_cuerpo("") is None
    assert extraer_pa_del_cuerpo(None) is None


def test_extraer_pa_con_dos_codigos_distintos_devuelve_none():
    """Ambigüedad real: sin esta guarda, cuál de los dos ganaba lo decidía un
    accidente del regex, no una regla. Elegir por accidente es identificar mal."""
    cuerpo = "asociados al 17844 - P.A SOL DE LA SIERRA y 18254 - P.A AUTOCONSUMO NESTLE del mes"
    assert extraer_pa_del_cuerpo(cuerpo) is None


def test_extraer_pa_repetido_el_mismo_codigo_si_identifica():
    """Repetir el MISMO P.A. no es ambiguo -- pasa en correos que lo nombran
    en el saludo y otra vez en el cuerpo."""
    cuerpo = ("17844 - P.A SOL DE LA SIERRA,\n"
              "los certificados del 17844 - P.A SOL DE LA SIERRA del mes de junio")
    assert extraer_pa_del_cuerpo(cuerpo) == {
        "codigo": "17844", "nombre": "P.A SOL DE LA SIERRA"}


# ── mandatos_enviados_en_correo ───────────────────────────────────────────────

def test_enviados_saca_un_cmu_por_adjunto():
    enviados = mandatos_enviados_en_correo(ADJUNTOS_REALES_DRIVE)
    assert [e["cmu"] for e in enviados] == [
        "CMU1135", "CMU1140", "CMU1147", "CMU1148"]
    assert all(e["tipo"] == "costo" for e in enviados)


def test_enviados_trae_el_proyecto_de_cada_adjunto():
    enviados = {e["cmu"]: e["proyecto"] for e in mandatos_enviados_en_correo(ADJUNTOS_REALES_DRIVE)}
    assert enviados["CMU1140"] == "Minigranja Solar Merengue"


def test_enviados_ignora_lo_que_no_es_pdf_de_mandato():
    assert mandatos_enviados_en_correo(["REGISTRO MANDATOS.xlsx", "foto.png"]) == []


def test_enviados_ignora_un_pdf_que_no_es_mandato():
    """Un PDF suelto sin la convención de nombre no es un mandato enviado.
    Sin esta guarda, cualquier adjunto inflaría el conteo de la reconciliación."""
    assert mandatos_enviados_en_correo(["cotizacion.pdf"]) == []


def test_enviados_sin_adjuntos():
    assert mandatos_enviados_en_correo([]) == []


# ── líneas de conformidad: NO son observaciones ───────────────────────────────
# Regla de negocio (Adhara, 2026-08-20): "un mandato firmado no debe tener
# correcciones". Vanessa manda correos mixtos: una observación real y, en otra
# línea, la confirmación de los mandatos que sí quedaron bien. Antes, toda línea
# con un CMU se convertía en observación y esos CMU conformes terminaban
# marcados con_comentarios (27 casos reales en la corrida del 2026-08-20).

def test_linea_que_confirma_firmados_no_genera_observacion():
    cuerpo = (
        "Revisando encuentro las siguientes observaciones:\n"
        "CMU1255 no se evidencia contabilizacion del arriendo.\n"
        "Los certificados CMU1160, CMU1161 y CMU1163 se encuentran "
        "debidamente firmados y sin novedad.\n"
        "Cordialmente")
    assert extraer_observaciones(cuerpo) == [
        {"cmu": "CMU1255", "observacion": "no se evidencia contabilizacion del arriendo"}]


def test_conformidad_antes_del_cmu_tambien_se_descarta():
    cuerpo = ("Se presentan diferencias.\n"
              "CMU0900 diferencia de 1.200.000 en el arriendo.\n"
              "Debidamente firmados y aprobados: CMU0901, CMU0902.\n")
    assert [o["cmu"] for o in extraer_observaciones(cuerpo)] == ["CMU0900"]


def test_conformidad_negada_si_es_observacion():
    """'no está firmado' es una observación real, no una confirmación.
    Sin la excepción por negación, el filtro se comería el hallazgo."""
    cuerpo = ("Se presentan diferencias.\n"
              "CMU0910 el mandato no esta firmado por el inversionista.\n")
    assert [o["cmu"] for o in extraer_observaciones(cuerpo)] == ["CMU0910"]


def test_conformidad_no_bloquea_una_linea_mixta():
    """Si una misma línea confirma y observa, se extrae: perder una
    observación real es peor que dejar pasar un CMU conforme."""
    cuerpo = ("Siguientes observaciones:\n"
              "CMU0920 firmado correctamente pero falta el soporte del predial.\n")
    assert [o["cmu"] for o in extraer_observaciones(cuerpo)] == ["CMU0920"]


# ── es_correo_de_correcciones ─────────────────────────────────────────────────

def test_reconoce_que_se_comparten_correcciones():
    assert es_correo_de_correcciones(
        "Hola Vanessa, te comparto los mandatos con correcciones. CMU1255, CMU1266")


def test_reconoce_correcciones_de_asientos_contables():
    assert es_correo_de_correcciones(
        "Comparto correcciones de los asientos contables para CMU1270")


def test_no_confunde_una_observacion_con_una_correccion():
    """El correo de Vanessa REPORTA diferencias; no las corrige. Confundirlos
    marcaría como corregido justo lo que acaba de ser observado."""
    assert not es_correo_de_correcciones(REVISORIA_OBSERVACIONES)


def test_no_confunde_pedir_correccion_con_haberla_hecho():
    assert not es_correo_de_correcciones(
        "Por favor realizar las correcciones correspondientes en CMU1255")


def test_correcciones_vacio():
    assert not es_correo_de_correcciones("")
    assert not es_correo_de_correcciones(None)


# ── correos de correcciones REALES (Adhara → Vanessa, 2026-08-20) ─────────────

def test_correo_real_mandatos_faltantes_y_corregidos():
    assert es_correo_de_correcciones(
        "Espero te encuentres muy bien. Comparto los mandatos faltantes y "
        "corregidos, así como, el apunte contable. Estoy atenta a tus "
        "comentarios. Muchas gracias")


def test_correo_real_apuntes_ya_corregidos():
    assert es_correo_de_correcciones(
        "Muchas gracias por la información. Te comparto nuevamente los mandatos "
        "y los apuntes contables ya corregidos.")


def test_pedir_corregir_no_es_entregar_corregido():
    """'corregir' es una petición; 'corregido' es una entrega. La diferencia
    está en dos letras y separa dos hechos opuestos."""
    assert not es_correo_de_correcciones(
        "Adjunto el certificado. Por favor corregir el arriendo de CMU1255.")
