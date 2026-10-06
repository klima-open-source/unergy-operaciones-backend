"""Decisión de qué escribir en finanzas_mandatos a partir de un correo. Pura."""
from types import SimpleNamespace as NS
from datetime import date, datetime, timezone

from app.services.mandatos.finanzas_sync import (
    FUENTE_ENVIO, FUENTE_REVISORIA, FUENTE_SALIENTE, _aplicar, decidir_finanzas,
)
from app.services.mandatos.imap_client import CorreoCrudo
from tests.fixtures_mandatos_correos import ENVIO_INVERSIONISTA, REVISORIA_SEGUIMIENTO

AHORA = datetime(2026, 8, 18, 10, 0, tzinfo=timezone.utc)
PDF_FIRMADO = b"%PDF-firmado"
PDF_SIN = b"%PDF-sin"


def _correo(cuerpo, adjuntos=(), asunto="Certificados junio 2026", remitente="x@y.com"):
    return CorreoCrudo(message_id="<t@test>", fecha=AHORA, remitente=remitente,
                       asunto=asunto, cuerpo=cuerpo, adjuntos=list(adjuntos))


def _firmas_fake(resultado):
    return lambda _contenido: {"lineas": 2, "firmadas": 2 if resultado else 0,
                               "estado": "firmado_completo" if resultado else "sin_firmas"}


def test_pdf_firmado_de_la_revisoria_da_firmado():
    c = _correo("Adjunto los certificados firmados.",
                [("CMU1140-Mandato-Costos-Minigranja Solar Merengue.pdf", PDF_FIRMADO)])
    d = decidir_finanzas(c, FUENTE_REVISORIA, verificador=_firmas_fake(True))
    assert len(d["acciones"]) == 1
    a = d["acciones"][0]
    assert a["estado"] == "firmado"
    assert a["cmu"] == "CMU1140"
    assert a["proyecto"] == "Minigranja Solar Merengue"
    assert a["tipo"] == "costo"


def test_pdf_sin_firmas_no_se_marca_firmado():
    """El PDF llegó, pero abrirlo dice que no está firmado. Manda el documento,
    no el hecho de que haya adjunto."""
    c = _correo("Adjunto.", [("CMU1140-Mandato-Costos-X.pdf", PDF_SIN)])
    d = decidir_finanzas(c, FUENTE_REVISORIA, verificador=_firmas_fake(False))
    assert d["acciones"] == []
    assert d["requiere_revision"] is True


def test_correo_de_seguimiento_no_se_interpreta():
    c = _correo(REVISORIA_SEGUIMIENTO)
    d = decidir_finanzas(c, FUENTE_REVISORIA, verificador=_firmas_fake(True))
    assert d["acciones"] == []
    assert d["requiere_revision"] is True


def test_envio_a_inversionista_usa_el_pa_del_cuerpo_como_tercero():
    c = _correo(ENVIO_INVERSIONISTA,
                [("CMU1135-Mandato-Costos-Minigranja Solar La Paz Levende.pdf", PDF_FIRMADO)],
                remitente="jessica@unergy.io")
    d = decidir_finanzas(c, FUENTE_ENVIO, verificador=_firmas_fake(True))
    a = d["acciones"][0]
    assert a["estado"] == "enviado_inversionista"
    assert a["tercero"] == "P.A SOL DE LA SIERRA"
    assert a["periodo"] == date(2026, 6, 1)


def test_sin_pa_en_el_cuerpo_no_se_inventa_identidad():
    """Sin tercero no hay identidad completa, y no se inventa una.

    Antes esto se comprobaba exigiendo CERO acciones, porque se creía que el
    P.A. era el caso normal. No lo es: es UNO (Sol de la Sierra, 8 proyectos).
    La mayoría de los mandatos no tiene tercero, así que descartar el adjunto
    dejaba sin registrar casi todo. Hoy se resuelve por CMU, y la garantía se
    comprueba donde importa: sin tercero ni periodo, _aplicar no crea filas.
    """
    c = _correo("Adjunto los certificados de junio.",
                [("CMU1135-Mandato-Costos-X.pdf", PDF_FIRMADO)],
                remitente="jessica@unergy.io")
    d = decidir_finanzas(c, FUENTE_ENVIO, verificador=_firmas_fake(True))
    assert [(a["cmu"], a["tercero"], a["periodo"]) for a in d["acciones"]] == [
        ("CMU1135", None, None)]


def test_sin_periodo_en_el_asunto_no_se_inventa():
    c = _correo(ENVIO_INVERSIONISTA,
                [("CMU1135-Mandato-Costos-X.pdf", PDF_FIRMADO)],
                asunto="RE: sin mes", remitente="jessica@unergy.io")
    d = decidir_finanzas(c, FUENTE_ENVIO, verificador=_firmas_fake(True))
    assert [a["periodo"] for a in d["acciones"]] == [None]


class _DBFake:
    """Sesión mínima: devuelve la fila que se le configure."""

    def __init__(self, fila):
        self._fila = fila

    def query(self, _modelo):
        return self

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self._fila


def test_reaplicar_el_mismo_estado_no_es_error():
    """Caso real: 40 de 61 acciones de la primera tanda eran firmado→firmado.

    Volver a recibir el PDF firmado de un mandato ya firmado es idempotencia,
    no un conflicto. Reportarlo como transicion_invalida llena el panel de
    revisión de ruido y esconde los conflictos reales.
    """
    fila = NS(id=7, cmu="CMU1270", estado="firmado", periodo=None, tipo="costo",
              drive_url="https://drive/x", comentario=None, correo_ref=None,
              fecha_firma=None)
    accion = {"cmu": "CMU1270", "estado": "firmado", "periodo": None,
              "adjunto": None, "comentario": None}
    correo = NS(message_id="<x@test>", fecha=AHORA, adjuntos=[])
    r = _aplicar(_DBFake(fila), accion, correo)
    assert r["resultado"] == "sin_cambio"
    assert fila.estado == "firmado"


def test_saliente_registra_el_envio_aunque_el_pdf_no_este_firmado():
    """Un mandato que va HACIA la revisoría está sin firmar por definición --
    justamente se manda para que lo firmen. Antes esto no producía nada y la
    reconciliación se quedaba sin denominador."""
    c = _correo("Adjunto los mandatos de julio para revisión.",
                [("CMU1255-Mandato-Costos-Minigranja Solar Esmeralda-STRADA ASOCIADOS S A S.pdf",
                  PDF_SIN)],
                asunto="Revisión mandatos de costos - Julio")
    d = decidir_finanzas(c, FUENTE_SALIENTE, verificador=_firmas_fake(False))
    assert len(d["acciones"]) == 1
    a = d["acciones"][0]
    assert a["estado"] == "sin_firma"
    assert a["cmu"] == "CMU1255"
    assert a["tercero"] == "STRADA ASOCIADOS S A S"


def test_saliente_ignora_adjuntos_que_no_son_mandato():
    c = _correo("Adjunto.", [("Liquidacion_CoxEnergy_Jul2026.pdf", PDF_SIN)],
                asunto="Revisión mandatos de costos - Julio")
    d = decidir_finanzas(c, FUENTE_SALIENTE, verificador=_firmas_fake(False))
    assert d["acciones"] == []


def test_saliente_sin_periodo_en_el_asunto_no_inventa():
    """Sin periodo no se puede construir identidad, y no se inventa.

    Antes esto se comprobaba exigiendo CERO acciones. Ya no: los correos de
    lote hacia la revisoría tampoco traen identidad y sí deben registrar su
    envío (ver test_lote_sin_pa_registra_el_envio_por_cmu). Lo que se garantiza
    hoy es lo mismo de siempre, pero comprobado donde de verdad importa: la
    acción sale SIN periodo, y una acción sin periodo nunca crea una fila --
    _aplicar solo busca la existente y responde cmu_no_encontrado si no está.
    """
    c = _correo("Adjunto.",
                [("CMU1255-Mandato-Costos-Esmeralda-STRADA ASOCIADOS S A S.pdf", PDF_SIN)],
                asunto="Re: sin mes")
    d = decidir_finanzas(c, FUENTE_SALIENTE, verificador=_firmas_fake(False))
    assert [a["periodo"] for a in d["acciones"]] == [None]
    assert [a["tercero"] for a in d["acciones"]] == [None]


# ── buzones múltiples ─────────────────────────────────────────────────────────

def test_buzones_lista_los_configurados(monkeypatch):
    """Parte del correo de mandatos no pasa por adhara@: algunos envíos a la
    revisoría salen de la cuenta de Jessica y viven en SU carpeta de Enviados.
    Sin leer ese buzón, la reconciliación nunca sabe que salieron."""
    from apps.mandatos.services.imap_client import buzones

    monkeypatch.setenv("MANDATOS_IMAP_USER", "adhara@unergy.io")
    monkeypatch.setenv("MANDATOS_IMAP_PASSWORD", "x")
    monkeypatch.setenv("MANDATOS_IMAP_USER_2", "jessica@unergy.io")
    monkeypatch.setenv("MANDATOS_IMAP_PASSWORD_2", "y")
    assert buzones() == [("adhara@unergy.io", "x"), ("jessica@unergy.io", "y")]


def test_buzones_omite_el_segundo_si_no_esta_configurado(monkeypatch):
    """El segundo buzón es opcional: sin él todo funciona igual, solo con
    menos cobertura."""
    from apps.mandatos.services.imap_client import buzones

    monkeypatch.setenv("MANDATOS_IMAP_USER", "adhara@unergy.io")
    monkeypatch.setenv("MANDATOS_IMAP_PASSWORD", "x")
    monkeypatch.setenv("MANDATOS_IMAP_USER_2", "")
    monkeypatch.setenv("MANDATOS_IMAP_PASSWORD_2", "")
    assert buzones() == [("adhara@unergy.io", "x")]


def test_buzones_vacio_sin_credenciales(monkeypatch):
    from apps.mandatos.services.imap_client import buzones

    for k in ("MANDATOS_IMAP_USER", "MANDATOS_IMAP_PASSWORD",
              "MANDATOS_IMAP_USER_2", "MANDATOS_IMAP_PASSWORD_2"):
        monkeypatch.setenv(k, "")
    assert buzones() == []


def test_segundo_buzon_reusa_smtp_password_si_es_la_misma_cuenta(monkeypatch):
    """Sin duplicar el secreto: si el segundo buzón ES la cuenta de envío, se
    reusa SMTP_PASSWORD. Dos copias de la misma contraseña se desincronizan al
    rotarla y una de las dos deja de servir sin que nadie lo note."""
    from apps.mandatos.services.imap_client import buzones

    monkeypatch.setenv("MANDATOS_IMAP_USER", "adhara@unergy.io")
    monkeypatch.setenv("MANDATOS_IMAP_PASSWORD", "clave-adhara")
    monkeypatch.setenv("MANDATOS_IMAP_USER_2", "operaciones@unergy.io")
    monkeypatch.setenv("MANDATOS_IMAP_PASSWORD_2", "")
    monkeypatch.setenv("SMTP_USER", "operaciones@unergy.io")
    monkeypatch.setenv("SMTP_PASSWORD", "clave-operaciones")
    assert buzones() == [("adhara@unergy.io", "clave-adhara"),
                         ("operaciones@unergy.io", "clave-operaciones")]


def test_no_hereda_la_cuenta_de_envio_si_cambio(monkeypatch):
    """El fallback exige que el usuario coincida. Si alguien mueve el envío a
    otra dirección, el buzón se omite y el log lo dice -- en vez de ponerse a
    leer calladito una cuenta que nadie eligió."""
    from apps.mandatos.services.imap_client import buzones

    monkeypatch.setenv("MANDATOS_IMAP_USER", "adhara@unergy.io")
    monkeypatch.setenv("MANDATOS_IMAP_PASSWORD", "clave-adhara")
    monkeypatch.setenv("MANDATOS_IMAP_USER_2", "operaciones@unergy.io")
    monkeypatch.setenv("MANDATOS_IMAP_PASSWORD_2", "")
    monkeypatch.setenv("SMTP_USER", "noreply@unergy.io")
    monkeypatch.setenv("SMTP_PASSWORD", "clave-de-otra-cuenta")
    assert buzones() == [("adhara@unergy.io", "clave-adhara")]


def test_password_propia_del_segundo_buzon_manda_sobre_el_fallback(monkeypatch):
    from apps.mandatos.services.imap_client import buzones

    monkeypatch.setenv("MANDATOS_IMAP_USER", "adhara@unergy.io")
    monkeypatch.setenv("MANDATOS_IMAP_PASSWORD", "clave-adhara")
    monkeypatch.setenv("MANDATOS_IMAP_USER_2", "operaciones@unergy.io")
    monkeypatch.setenv("MANDATOS_IMAP_PASSWORD_2", "clave-propia")
    monkeypatch.setenv("SMTP_USER", "operaciones@unergy.io")
    monkeypatch.setenv("SMTP_PASSWORD", "clave-smtp")
    assert buzones()[1] == ("operaciones@unergy.io", "clave-propia")


# ── clasificación por destino ─────────────────────────────────────────────────

def test_correo_de_jessica_hacia_la_revisoria_es_un_envio():
    """Caso real: 'Revisión de mandatos autoconsumo - Julio', mandado por Jessica
    a Vanessa con copia a Adhara. Llega al INBOX como correo de Jessica, así que
    antes se trataba como envío a inversionista y su envío nunca se registraba
    -- 80 CMU de julio quedaron sin denominador por esto."""
    c = _correo("Adjunto los mandatos de autoconsumo para revisión.",
                [("CMU1182-Mandato-Iml Empaques Colombia Sas-Ayurá S.A.S.pdf", PDF_SIN)],
                asunto="Revisión de mandatos autoconsumo - Julio",
                remitente="jessica@unergy.io")
    c.destinatarios = "vlondono@jbp.com.co, adhara@unergy.io"
    d = decidir_finanzas(c, FUENTE_ENVIO, verificador=_firmas_fake(False))
    assert [a["estado"] for a in d["acciones"]] == ["sin_firma"]


def test_correo_de_jessica_a_un_inversionista_sigue_siendo_envio():
    """Sin la revisoría entre destinatarios, se comporta como antes."""
    c = _correo(ENVIO_INVERSIONISTA,
                [("CMU1135-Mandato-Costos-Minigranja Solar La Paz-Levende.pdf", PDF_FIRMADO)],
                remitente="jessica@unergy.io")
    c.destinatarios = "juliana@solenium.co, adhara@unergy.io"
    d = decidir_finanzas(c, FUENTE_ENVIO, verificador=_firmas_fake(True))
    assert [a["estado"] for a in d["acciones"]] == ["enviado_inversionista"]


def test_sin_destinatarios_se_comporta_como_antes():
    """Los correos ya registrados no traen destinatarios. No deben cambiar de
    interpretación solo porque el campo llegue vacío."""
    c = _correo(ENVIO_INVERSIONISTA,
                [("CMU1135-Mandato-Costos-Minigranja Solar La Paz-Levende.pdf", PDF_FIRMADO)],
                remitente="jessica@unergy.io")
    d = decidir_finanzas(c, FUENTE_ENVIO, verificador=_firmas_fake(True))
    assert [a["estado"] for a in d["acciones"]] == ["enviado_inversionista"]


# ── estado `corregido` ────────────────────────────────────────────────────────

def test_un_correo_de_correcciones_marca_corregido():
    """Confirmado con el usuario: la corrección aplica a los CMU que el correo
    nombra. Sin esto, con_comentarios era un callejón sin salida -- nada emitía
    `corregido`, así que un mandato observado no podía volver a firmarse."""
    c = _correo("Hola Vanessa, te comparto los mandatos con correcciones: "
                "CMU1255, CMU1266 y CMU1270.",
                asunto="Re: Revisión mandatos de costos - Julio",
                remitente="adhara@unergy.io")
    c.destinatarios = "vlondono@jbp.com.co"
    d = decidir_finanzas(c, FUENTE_SALIENTE, verificador=_firmas_fake(False))
    assert sorted(a["cmu"] for a in d["acciones"]) == ["CMU1255", "CMU1266", "CMU1270"]
    assert all(a["estado"] == "corregido" for a in d["acciones"])


def test_un_saliente_sin_lenguaje_de_correccion_no_marca_corregido():
    c = _correo("Adjunto los mandatos de julio para su revisión.",
                [("CMU1255-Mandato-Costos-Esmeralda-STRADA ASOCIADOS S A S.pdf", PDF_SIN)],
                asunto="Revisión mandatos de costos - Julio")
    c.destinatarios = "vlondono@jbp.com.co"
    d = decidir_finanzas(c, FUENTE_SALIENTE, verificador=_firmas_fake(False))
    assert [a["estado"] for a in d["acciones"]] == ["sin_firma"]


def test_correcciones_sin_cmu_nombrado_no_inventa():
    """Si el correo dice que comparte correcciones pero no nombra ninguno, no se
    adivina a cuáles aplica: se deja para revisión."""
    c = _correo("Te comparto los mandatos con correcciones.",
                asunto="Re: Revisión mandatos de costos - Julio")
    c.destinatarios = "vlondono@jbp.com.co"
    d = decidir_finanzas(c, FUENTE_SALIENTE, verificador=_firmas_fake(False))
    assert d["acciones"] == []
    assert d["requiere_revision"] is True


def test_correcciones_no_toca_los_cmu_citados_del_hilo():
    """El correo de correcciones casi siempre responde al que traía las
    observaciones. Sin recortar la cita se marcarían como corregidos CMU que
    solo aparecen en el historial del hilo."""
    c = _correo("Te comparto las correcciones de CMU1255.\n"
                "> CMU1266 no se evidencia contabilizacion\n"
                "> CMU1270 diferencia en el arriendo\n",
                asunto="Re: Revisión mandatos de costos - Julio")
    c.destinatarios = "vlondono@jbp.com.co"
    d = decidir_finanzas(c, FUENTE_SALIENTE, verificador=_firmas_fake(False))
    assert [a["cmu"] for a in d["acciones"]] == ["CMU1255"]


# ── ruido: adjuntos que no son mandatos ───────────────────────────────────────

def test_una_factura_adjunta_no_pide_revision():
    """Caso real: Jessica manda facturas y comprobantes de pago a los mismos
    destinatarios. Antes, cualquier PDF que no encajara levantaba la bandera de
    revisión -- 136 de 385 correos en la corrida del 2026-08-20. El panel se
    llenaba de facturas y dejaba de servir."""
    c = _correo("Adjunto la factura por servicios de julio.",
                [("Factura_UESP2166_Servicios_Nestle.pdf", PDF_SIN)],
                asunto="18254 - P.A. AUTOCONSUMO NESTLE: Facturas por servicios - Julio",
                remitente="jessica@unergy.io")
    d = decidir_finanzas(c, FUENTE_ENVIO, verificador=_firmas_fake(False))
    assert d["requiere_revision"] is False
    assert d["sin_identidad"] == []
    assert d["ignorados"] == ["Factura_UESP2166_Servicios_Nestle.pdf"]


def test_un_mandato_ilegible_si_pide_revision():
    """Lo contrario: el archivo dice ser un mandato pero su nombre no se pudo
    interpretar. Eso sí hay que mirarlo -- es un mandato que se está perdiendo."""
    c = _correo("Adjunto el mandato.",
                [("Mandato costos julio version final.pdf", PDF_SIN)],
                remitente="jessica@unergy.io")
    d = decidir_finanzas(c, FUENTE_ENVIO, verificador=_firmas_fake(False))
    assert d["requiere_revision"] is True
    assert d["sin_identidad"] == ["Mandato costos julio version final.pdf"]


def test_un_pdf_con_cmu_en_el_nombre_pide_revision():
    """Si trae un CMU, pretende ser un mandato aunque el resto del nombre no
    siga ninguna convención conocida."""
    c = _correo("Adjunto.", [("CMU9999 corregido.pdf", PDF_SIN)],
                remitente="jessica@unergy.io")
    d = decidir_finanzas(c, FUENTE_ENVIO, verificador=_firmas_fake(False))
    assert d["sin_identidad"] == ["CMU9999 corregido.pdf"]


def test_la_bitacora_guarda_la_fuente_efectiva():
    """Un correo de Jessica hacia la revisoría se reclasifica; la fuente que se
    reporta debe ser la reclasificada, no la de la pasada."""
    c = _correo("Adjunto los mandatos para revisión.",
                [("CMU1182-Mandato-Iml Empaques Colombia Sas-Ayurá S.A.S.pdf", PDF_SIN)],
                remitente="jessica@unergy.io")
    c.destinatarios = "vlondono@jbp.com.co"
    d = decidir_finanzas(c, FUENTE_ENVIO, verificador=_firmas_fake(False))
    assert d["fuente_efectiva"] == FUENTE_SALIENTE


# ── correos de LOTE hacia la revisoría (sin P.A.) ─────────────────────────────

def test_lote_sin_pa_registra_el_envio_por_cmu():
    """Caso real: 'Revisión de mandatos autoconsumo - Julio' lleva 23 mandatos
    de 23 empresas distintas y por eso NO trae P.A. Exigir identidad completa
    dejó 27 mandatos de julio sin registrar su envío."""
    c = _correo("Adjunto los mandatos de autoconsumo para revisión.",
                [("CMU1170-Mandato-Edificio Torre Almagran Propiedad Horizontal.pdf", PDF_SIN),
                 ("CMU1160-Mandato-Almacen Amc Sas.pdf", PDF_SIN)],
                asunto="Revisión de mandatos autoconsumo - Julio")
    d = decidir_finanzas(c, FUENTE_SALIENTE, verificador=_firmas_fake(False))
    assert d["sin_identidad"] == []
    assert sorted(a["cmu"] for a in d["acciones"]) == ["CMU1160", "CMU1170"]
    assert all(a["estado"] == "sin_firma" for a in d["acciones"])
    # Sin periodo: _aplicar debe resolverlas por CMU contra la fila existente.
    assert all(a["periodo"] is None for a in d["acciones"])


def test_lote_de_correcciones_propone_corregido_con_alterno():
    """'Comparto los mandatos faltantes y corregidos' mezcla las dos cosas: se
    propone `corregido` y se deja `sin_firma` de alterno para los faltantes."""
    c = _correo("Comparto los mandatos faltantes y corregidos, así como, el "
                "apunte contable.",
                [("CMU1255-Mandato-Costos-Esmeralda.pdf", PDF_SIN)],
                asunto="Re: Revisión mandatos de costos - Julio")
    d = decidir_finanzas(c, FUENTE_SALIENTE, verificador=_firmas_fake(False))
    a = d["acciones"][0]
    assert (a["estado"], a["estado_alterno"]) == ("corregido", "sin_firma")


def test_accion_sin_periodo_de_un_cmu_inexistente_no_crea_nada():
    """La garantía de fondo del lote sin P.A.: si el CMU no existe, se reporta
    y no se crea nada. Es la anomalía que la reconciliación llama
    sin_registro_de_envio, y taparla con una fila incompleta la escondería."""
    correo = NS(message_id="<x@test>", fecha=AHORA, adjuntos=[])
    accion = {"cmu": "CMU9999", "estado": "sin_firma", "periodo": None,
              "adjunto": None, "comentario": None}
    db = _DBFake(None)
    r = _aplicar(db, accion, correo)
    assert r == {"cmu": "CMU9999", "resultado": "cmu_no_encontrado"}
    assert not hasattr(db, "_agregado")


def test_estado_alterno_se_usa_cuando_el_destino_no_cabe():
    """Un 'faltante' dentro de un correo de correcciones: está en sin_firma,
    `corregido` no cabe desde ahí, y el alterno sin_firma sí -- ya registrado,
    así que sin_cambio. Sin el alterno saldría transicion_invalida."""
    fila = NS(id=9, cmu="CMU1300", estado="sin_firma", periodo=None, tipo="costo",
              drive_url=None, comentario=None, correo_ref=None, fecha_firma=None)
    accion = {"cmu": "CMU1300", "estado": "corregido", "estado_alterno": "sin_firma",
              "periodo": None, "adjunto": None, "comentario": None}
    correo = NS(message_id="<x@test>", fecha=AHORA, adjuntos=[])
    assert _aplicar(_DBFake(fila), accion, correo)["resultado"] == "sin_cambio"


def test_estado_alterno_no_estorba_cuando_el_destino_si_cabe():
    """Un mandato observado sí puede pasar a corregido: el alterno no debe
    desviarlo."""
    fila = NS(id=9, cmu="CMU1287", estado="con_comentarios", periodo=None,
              tipo="costo", drive_url=None, comentario="falta soporte",
              correo_ref=None, fecha_firma=None)
    accion = {"cmu": "CMU1287", "estado": "corregido", "estado_alterno": "sin_firma",
              "periodo": None, "adjunto": None, "comentario": None}
    correo = NS(message_id="<x@test>", fecha=AHORA, adjuntos=[])
    r = _aplicar(_DBFake(fila), accion, correo)
    assert (r["resultado"], fila.estado) == ("aplicado", "corregido")


def test_autoconsumo_sin_tercero_registra_el_envio_al_inversionista():
    """En autoconsumo la empresa que firma ES el proyecto: no hay tercero, y el
    correo tampoco trae P.A. Exigirlo dejaba estos mandatos sin registrar."""
    c = _correo("Adjunto el certificado firmado.",
                [("CMU1170-Mandato-Edificio Torre Almagran Propiedad Horizontal.pdf",
                  PDF_FIRMADO)],
                asunto="Certificado de mandato Autoconsumo - Julio",
                remitente="jessica@unergy.io")
    d = decidir_finanzas(c, FUENTE_ENVIO, verificador=_firmas_fake(True))
    assert d["sin_identidad"] == []
    a = d["acciones"][0]
    assert (a["cmu"], a["estado"], a["tercero"]) == (
        "CMU1170", "enviado_inversionista", None)
    # El adjunto se conserva: es el PDF firmado que va a Drive.
    assert a["adjunto"] == "CMU1170-Mandato-Edificio Torre Almagran Propiedad Horizontal.pdf"


def test_con_pa_sigue_armando_la_identidad_completa():
    """Sol de la Sierra es el caso que SÍ tiene P.A. No debe perder identidad
    por el camino nuevo."""
    c = _correo(ENVIO_INVERSIONISTA,
                [("CMU1135-Mandato-Costos-Minigranja Solar La Paz.pdf", PDF_FIRMADO)],
                remitente="jessica@unergy.io")
    d = decidir_finanzas(c, FUENTE_ENVIO, verificador=_firmas_fake(True))
    a = d["acciones"][0]
    assert a["tercero"] == "P.A SOL DE LA SIERRA"
    assert a["periodo"] == date(2026, 6, 1)


# ── firma incompleta devuelta por la revisoría ────────────────────────────────

def _firmas_parciales():
    return lambda _c: {"lineas": 2, "firmadas": 1, "estado": "parcial",
                       "tipo": "ingreso"}


def test_devuelto_firmado_pero_le_falta_una_firma_queda_observado():
    """Caso real: CMU1168 - Dual Cross S.A.S. volvió en el correo del 12 de
    agosto como firmado, pero el PDF trae 1 de 2 firmas. Antes solo caía en la
    lista de revisión del correo y se perdía; ahora queda pegado al mandato."""
    c = _correo("Adjunto los certificados firmados.",
                [("CMU1168-Mandato-Dual Cross S.A.S.pdf", PDF_SIN)])
    d = decidir_finanzas(c, FUENTE_REVISORIA, verificador=_firmas_parciales())
    a = d["acciones"][0]
    assert (a["cmu"], a["estado"]) == ("CMU1168", "con_comentarios")
    assert "1 de 2 firmas" in a["comentario"]
    # No se sube a Drive un documento incompleto.
    assert a["adjunto"] is None
    # Ya no ensucia la lista de revisión: el hallazgo tiene dónde vivir.
    assert d["sin_identidad"] == []


def test_un_pdf_ilegible_de_la_revisoria_sigue_pidiendo_revision():
    """'No pude abrirlo' no es 'le falta una firma'. Sin conclusión posible
    sobre el mandato, se marca para que alguien lo mire."""
    c = _correo("Adjunto.", [("CMU1168-Mandato-Dual Cross S.A.S.pdf", PDF_SIN)])
    verificador = lambda _c: {"lineas": 0, "firmadas": 0,
                              "estado": "no_verificable", "tipo": "costo"}
    d = decidir_finanzas(c, FUENTE_REVISORIA, verificador=verificador)
    assert d["acciones"] == []
    assert d["requiere_revision"] is True


def test_registrar_envio_por_cmu_no_degrada_un_mandato_firmado():
    """46 envíos se rechazaron como 'firmado → sin_firma' en la corrida del
    2026-08-20. Un mandato ya firmado que reaparece en el correo de lote que lo
    mandó a revisión sigue firmado: lo que se anota es que salió."""
    fila = NS(id=11, cmu="CMU1255", estado="firmado", periodo=None, tipo="costo",
              drive_url=None, comentario=None, correo_ref=None,
              fecha_firma=None, fecha_envio=None)
    accion = {"cmu": "CMU1255", "estado": "sin_firma", "periodo": None,
              "adjunto": None, "comentario": None}
    correo = NS(message_id="<x@test>", fecha=AHORA, adjuntos=[])
    r = _aplicar(_DBFake(fila), accion, correo)
    assert r["resultado"] == "aplicado"
    assert r["solo_fecha_envio"] is True
    assert fila.estado == "firmado"          # no retrocede
    assert fila.fecha_envio == AHORA.date()  # pero queda registrado el envío


def test_registrar_envio_por_cmu_no_pisa_una_fecha_de_envio_previa():
    fila = NS(id=11, cmu="CMU1255", estado="firmado", periodo=None, tipo="costo",
              drive_url=None, comentario=None, correo_ref=None, fecha_firma=None,
              fecha_envio=date(2026, 7, 1))
    accion = {"cmu": "CMU1255", "estado": "sin_firma", "periodo": None,
              "adjunto": None, "comentario": None}
    _aplicar(_DBFake(fila), accion, NS(message_id="<x@test>", fecha=AHORA, adjuntos=[]))
    assert fila.fecha_envio == date(2026, 7, 1)


def test_un_cmu_nombrado_en_un_correo_de_correcciones_lleva_alterno():
    """Sin alterno, un CMU nombrado en el texto que aún está en sin_firma
    salía rechazado como 'sin_firma → corregido'."""
    c = _correo("Te comparto los mandatos ya corregidos: CMU1300.",
                asunto="Re: Revisión mandatos de costos - Julio")
    c.destinatarios = "vlondono@jbp.com.co"
    d = decidir_finanzas(c, FUENTE_SALIENTE, verificador=_firmas_fake(False))
    assert d["acciones"][0]["estado_alterno"] == "sin_firma"
