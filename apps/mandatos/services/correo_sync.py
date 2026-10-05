"""Correo → qué escribir en finanzas_mandatos.

Puerto de `app/services/mandatos/finanzas_sync.py`. Era la última pieza que
`api/v1/mandatos/` seguía consumiendo de FastAPI, así que portarla cierra
también esa escotilla a SQLAlchemy.

Compone piezas ya probadas: el parser interpreta el texto, el detector de firmas
mira el PDF, el servicio de Finanzas escribe. Acá solo se decide.

`decidir_finanzas` es pura: recibe un correo y un verificador de firmas
inyectado, y devuelve qué habría que hacer. No consulta la base ni escribe
archivos, para poder probar las reglas sin montar un arnés.

Los cuatro módulos que usa —`adjuntos`, `email_parser`, `firmas`, `imap_client`,
900 líneas— viven al lado, en `apps/mandatos/services/`, copiados tal cual de
`app/services/mandatos/`: no tocan la base ni saben de framework.
"""
from __future__ import annotations

import logging
from datetime import date

from apps.mandatos.services.finanzas import (
    extraer_periodo_de_asunto, tipo_de_nombre,
)
from apps.mandatos.services.adjuntos import expandir_adjuntos
from apps.mandatos.services.email_parser import (
    CLASIF_MOLDE_SIMPLE, _sin_cita, clasificar_correo, es_correo_de_correcciones,
    extraer_observaciones, extraer_pa_del_cuerpo, parece_nombre_de_mandato,
)
from apps.mandatos.services.firmas import verificar_firmas
from apps.mandatos.services.imap_client import CorreoCrudo
from apps.mandatos.services.reglas import extraer_cmus, parsear_nombre_zip

logger = logging.getLogger("operaciones.mandatos.correo")

FUENTE_REVISORIA = "revisoria"
FUENTE_ENVIO = "envio_inversionista"
# Lo que SALE hacia la revisoría. Necesita su propia fuente y no reusar
# FUENTE_REVISORIA: son direcciones opuestas y significan lo contrario. Un
# mandato entrante firmado dice "ya está listo"; uno saliente dice "acaba de
# pedirse la firma", y está sin firmar por definición.
FUENTE_SALIENTE = "saliente_revisoria"


def _identidad(nombre_archivo: str, correo: CorreoCrudo,
               tipo: str | None = None) -> dict | None:
    """(cmu, proyecto, tercero, tipo, periodo) o None si falta algo.

    Devolver None es lo NORMAL, no un error: la mayoría de los mandatos no
    tienen tercero. El tercero solo existe si el nombre del archivo trae
    inversionista o si el cuerpo trae un P.A., y el P.A. es un solo caso (Sol
    de la Sierra, 8 proyectos; verificado con el usuario 2026-08-20). En
    autoconsumo la empresa que firma ES el proyecto.

    Cuando devuelve None, quien llama debe resolver por CMU (ver _por_cmu), no
    descartar el adjunto. Lo que nunca se hace es completar el tercero con un
    valor por defecto: la identidad es la llave única de la tabla, y una
    identidad inventada crea una fila fantasma que después nadie reconoce.
    """
    parsed = parsear_nombre_zip(nombre_archivo)
    if not parsed:
        return None
    periodo = extraer_periodo_de_asunto(correo.asunto or "", correo.fecha.date())
    if not periodo:
        return None
    pa = extraer_pa_del_cuerpo(correo.cuerpo)
    if not pa and parsed.get("pa_codigo"):
        # El lote de Sol de la Sierra trae el P.A. en el NOMBRE del archivo
        # ("... - 17844 SOL DE LA SIERRA.pdf"). Es la misma fuente de verdad y
        # llega igual aunque el cuerpo del correo no lo mencione.
        pa = {"codigo": parsed["pa_codigo"], "nombre": parsed["pa_nombre"]}
    tercero = parsed["inversionista"] or (pa["nombre"] if pa else "")
    if not tercero:
        return None
    return {
        "cmu": parsed["cmu"],
        "proyecto": parsed["proyecto"],
        "tercero": tercero,
        "tipo": tipo or tipo_de_nombre(nombre_archivo),
        "periodo": periodo,
        "pa_codigo": pa["codigo"] if pa else None,
    }


def _por_cmu(parsed: dict, nombre: str, estado: str, firmas: dict | None,
             *, estado_alterno: str | None = None, adjunto: str | None = None,
             tipo: str | None = None) -> dict:
    """Acción identificada SOLO por el CMU, que _aplicar resuelve contra la fila
    que ya exista.

    Es el camino normal, no la excepción. La identidad completa necesita un
    tercero, y el tercero solo aparece cuando el nombre del archivo trae
    inversionista o cuando el cuerpo trae un P.A. Verificado con el usuario
    (2026-08-20): el P.A. es UN caso -- Sol de la Sierra, con 8 proyectos. Todo
    lo demás son empresas individuales, y en autoconsumo la empresa que firma ES
    el proyecto, no hay un tercero aparte.

    Exigir identidad completa dejaba sin registrar todo lo que no encajara en
    esa excepción. El CMU ya es el número único del certificado: se resuelve por
    ahí en vez de inventar un tercero. Si la fila no existe, _aplicar responde
    cmu_no_encontrado y la reconciliación lo reporta -- que es justo la anomalía
    que hay que ver, no tapar con una fila a medias.
    """
    return {"cmu": parsed["cmu"], "estado": estado,
            "estado_alterno": estado_alterno,
            "proyecto": parsed["proyecto"],
            "tipo": tipo or tipo_de_nombre(nombre),
            "tercero": None, "periodo": None, "pa_codigo": None,
            "adjunto": adjunto, "firmas": firmas, "comentario": None}


def decidir_finanzas(correo: CorreoCrudo, fuente: str, *, verificador=verificar_firmas) -> dict:
    """{'clasificacion', 'acciones', 'requiere_revision', 'sin_identidad'}.

    `verificador` se inyecta para poder probar sin PDFs reales.
    """
    # Hacia dónde va el correo manda sobre quién lo mandó. Un correo de Jessica
    # con la revisoría entre destinatarios es una PETICIÓN DE FIRMA, no una
    # entrega a un inversionista, aunque salga de la misma persona. Clasificar
    # solo por remitente dejó 80 mandatos de julio (los de ingresos y
    # autoconsumo) sin registrar su envío.
    #
    # Si `destinatarios` viene vacío -- correos leídos antes de que se capturara
    # el campo -- se conserva la fuente original en vez de adivinar.
    if (fuente == FUENTE_ENVIO and correo.destinatarios
            and REMITENTE_REVISORIA in correo.destinatarios):
        fuente = FUENTE_SALIENTE

    adjuntos = expandir_adjuntos(list(correo.adjuntos))
    acciones: list[dict] = []
    sin_identidad: list[str] = []
    # Adjuntos que no pretenden ser mandatos (facturas, comprobantes de pago).
    # Se anotan para dejar rastro, pero NO piden revisión humana.
    ignorados: list[str] = []
    es_correcciones = (fuente == FUENTE_SALIENTE
                       and es_correo_de_correcciones(correo.cuerpo))
    clasificacion = clasificar_correo(correo.asunto, correo.cuerpo)

    for nombre, contenido in adjuntos:
        if not nombre.lower().endswith(".pdf"):
            continue
        parsed = parsear_nombre_zip(nombre)
        if not parsed:
            (sin_identidad if parece_nombre_de_mandato(nombre)
             else ignorados).append(nombre)
            continue
        firmas = verificador(contenido)
        # El tipo sale del CONTENIDO del PDF, no del nombre: un lote de
        # autoconsumo trae ingresos y costos con la misma convención de archivo
        # ("CMU####-Mandato-{Empresa}.pdf"), y el tipo es parte de la identidad
        # única. Clasificarlos igual los haría chocar en la misma fila. Si el
        # verificador no lo trae (tests con doble), se cae al nombre.
        tipo = firmas.get("tipo") or tipo_de_nombre(nombre)
        if fuente == FUENTE_SALIENTE:
            # Registrar el envío. No importa si el PDF está firmado -- casi
            # nunca lo estará. Lo que se registra es que salió, para que la
            # reconciliación tenga contra qué comparar lo que vuelve.
            #
            # Si el correo comparte correcciones, lo que sale no es un envío
            # nuevo sino la respuesta a unas observaciones: el destino natural
            # es `corregido`. Pero un mismo correo mezcla las dos cosas ("los
            # mandatos FALTANTES y corregidos"), y desde el texto no hay cómo
            # saber cuál es cuál. Se propone `corregido` y se deja `sin_firma`
            # como alterno: manda el estado en que esté el mandato, que es lo
            # único que distingue un faltante de un corregido.
            estado = "corregido" if es_correcciones else "sin_firma"
            alterno = "sin_firma" if es_correcciones else None
            ident = _identidad(nombre, correo, tipo)
            if ident:
                acciones.append({**ident, "estado": estado, "adjunto": None,
                                 "estado_alterno": alterno,
                                 "firmas": firmas, "comentario": None})
            else:
                acciones.append(_por_cmu(parsed, nombre, estado, firmas,
                                         estado_alterno=alterno, tipo=tipo))
        elif fuente == FUENTE_ENVIO:
            # Cuando el correo trae P.A. o el archivo trae inversionista se
            # arma la identidad completa y se puede CREAR la fila. Cuando no
            # -- que es lo normal, ver _por_cmu -- se resuelve por CMU y no se
            # crea nada: un mandato que llega al inversionista sin haberse
            # registrado nunca es la anomalía que hay que ver, no una fila que
            # inventar.
            ident = _identidad(nombre, correo, tipo)
            if ident:
                acciones.append({**ident, "estado": "enviado_inversionista",
                                 "adjunto": nombre, "firmas": firmas,
                                 "comentario": None})
            else:
                acciones.append(_por_cmu(parsed, nombre, "enviado_inversionista",
                                         firmas, adjunto=nombre, tipo=tipo))
        elif firmas["estado"] == "firmado_completo":
            # Los correos de la revisoría NO traen el P.A. -- verificado contra
            # los tres fixtures reales: extraer_pa_del_cuerpo devuelve None en
            # todos. Así que acá no se construye identidad, se busca la que ya
            # existe por CMU (ver _aplicar). Inventar un tercero vacío crearía
            # una fila paralela a la real y partiría el mandato en dos.
            acciones.append({"cmu": parsed["cmu"], "proyecto": parsed["proyecto"],
                             "tipo": tipo, "tercero": None,
                             "periodo": None, "pa_codigo": None, "estado": "firmado",
                             "adjunto": nombre, "firmas": firmas, "comentario": None})
        elif firmas["estado"] == "parcial":
            # La revisoría lo devolvió como firmado, pero al PDF le falta una de
            # las dos firmas (caso real: CMU1168 - Dual Cross S.A.S., 2026-08-12).
            # Eso es un hallazgo sobre el documento y tiene que quedar pegado al
            # mandato, no solo en la bitácora del correo: ahí se pierde apenas
            # pasa la corrida, y nadie se entera.
            #
            # Va como `con_comentarios` y no como estado propio a propósito: es
            # exactamente el ciclo que le toca -- vuelve a la revisoría, se
            # corrige y se firma -- y no obliga a tocar un enum que comparte el
            # módulo de Finanzas. El comentario dice de qué se trata para que no
            # se confunda con una observación contable.
            faltan = firmas["lineas"] - firmas["firmadas"]
            acciones.append({
                "cmu": parsed["cmu"], "estado": "con_comentarios",
                "comentario": (f"Devuelto como firmado, pero el PDF trae "
                               f"{firmas['firmadas']} de {firmas['lineas']} "
                               f"firmas (falta {faltan})."),
                "proyecto": parsed["proyecto"], "tipo": tipo, "tercero": None,
                "periodo": None, "pa_codigo": None, "adjunto": None,
                "estado_alterno": None, "firmas": firmas})
        else:
            # Ni firmado ni parcial: o llegó sin ninguna firma, o no se pudo
            # abrir el PDF. No hay conclusión que sacar sobre el mandato, así
            # que se marca para que alguien lo mire.
            sin_identidad.append(f"{nombre} ({firmas['estado']})")

    # Correcciones compartidas hacia la revisoría → `corregido`, para los CMU
    # que el correo nombra. Se leen del cuerpo SIN la cita del hilo: un correo
    # de correcciones casi siempre responde al que traía las observaciones, y
    # sin recortar se marcarían como corregidos los CMU citados de ese hilo.
    if es_correcciones:
        con_pdf = {a["cmu"] for a in acciones}
        nombrados = [c for c in extraer_cmus(_sin_cita(correo.cuerpo or ""))
                     if c not in con_pdf]
        for cmu in nombrados:
            # Mismo alterno que los CMU que vienen como adjunto: el correo
            # mezcla corregidos con faltantes y desde el texto no se distinguen.
            acciones.append({"cmu": cmu, "estado": "corregido", "comentario": None,
                             "estado_alterno": "sin_firma",
                             "adjunto": None, "proyecto": None, "tercero": None,
                             "tipo": None, "periodo": None, "pa_codigo": None,
                             "firmas": None})
        if not nombrados and not acciones:
            # Dice que comparte correcciones pero no nombra ninguno. No se
            # adivina el lote: se deja visible para que alguien lo mire.
            sin_identidad.append("correo de correcciones sin CMU identificable")

    # Observaciones de texto, solo si el correo encaja en el molde conocido.
    if fuente == FUENTE_REVISORIA and clasificacion == CLASIF_MOLDE_SIMPLE:
        con_pdf = {a["cmu"] for a in acciones}
        for obs in extraer_observaciones(correo.cuerpo):
            if obs["cmu"] in con_pdf:
                continue
            # Sin adjunto no hay nombre de archivo, así que no hay proyecto ni
            # tipo: la acción sale incompleta y el aplicador la resuelve por CMU
            # contra lo que ya exista en la tabla.
            acciones.append({"cmu": obs["cmu"], "estado": "con_comentarios",
                             "comentario": obs["observacion"], "adjunto": None,
                             "proyecto": None, "tercero": None, "tipo": None,
                             "periodo": None, "pa_codigo": None, "firmas": None})

    requiere = bool(sin_identidad) or (
        fuente == FUENTE_REVISORIA and clasificacion != CLASIF_MOLDE_SIMPLE)
    return {"clasificacion": clasificacion, "acciones": acciones,
            "requiere_revision": requiere, "sin_identidad": sin_identidad,
            "ignorados": ignorados, "fuente_efectiva": fuente}



def _aplicar(accion: dict, correo: CorreoCrudo) -> dict:
    """Aplica una acción y devuelve su registro para la bitácora.

    La validación de transiciones vive acá y no en `upsert`: esa función la
    sigue usando el script de Jessica por `/ingest`, y meterle una regla nueva
    podría rechazar algo que ella hace hoy. La red de seguridad va donde está la
    automatización.
    """
    from apps.mandatos.models import FinanzasMandato
    from apps.mandatos.services.finanzas import transicion_firma_valida
    from apps.mandatos.services.finanzas_upsert import upsert

    if not accion.get("periodo"):
        # Todo lo que viene de la revisoría cae acá: sus correos no traen el
        # P.A., así que no hay identidad que construir y hay que encontrar la
        # fila que ya existe. Aplica tanto a las observaciones de texto como a
        # los PDF firmados.
        #
        # Si no existe la fila, NO se crea: significaría que llegó firmado algo
        # que la plataforma nunca vio salir. Eso es justo la anomalía que la
        # reconciliación reporta como `sin_registro_de_envio`, y taparla creando
        # una fila incompleta la volvería invisible.
        existente = (FinanzasMandato.objects
                     .filter(cmu=accion["cmu"]).order_by("-periodo").first())
        if not existente:
            return {"cmu": accion["cmu"], "resultado": "cmu_no_encontrado"}
        previo = existente.estado
        destino = accion["estado"]
        # Estado alterno: la acción propone un destino pero admite otro si el
        # primero no cabe desde donde está el mandato. Lo usa el correo de
        # correcciones, que mezcla mandatos corregidos con mandatos que salen
        # por primera vez y no puede distinguirlos desde el texto.
        alterno = accion.get("estado_alterno")
        if (alterno and previo != destino
                and not transicion_firma_valida(previo, destino)
                and (previo == alterno
                     or transicion_firma_valida(previo, alterno))):
            destino = alterno
        if previo == destino:
            return {"cmu": accion["cmu"], "resultado": "sin_cambio",
                    "estado": destino, "id": existente.id}
        if not transicion_firma_valida(previo, destino):
            return {"cmu": accion["cmu"], "resultado": "transicion_invalida",
                    "estado_previo": previo, "estado_destino": destino}

        ya_tenia_pdf = bool(existente.drive_url)
        drive_id, drive_url = _subir_si_falta(accion, correo, ya_tenia_pdf,
                                              existente.periodo)
        existente.estado = destino
        campos = ["estado"]
        if accion.get("comentario"):
            existente.comentario = accion["comentario"]
            campos.append("comentario")
        if drive_url:
            existente.drive_file_id, existente.drive_url = drive_id, drive_url
            campos += ["drive_file_id", "drive_url"]
        existente.save(update_fields=campos)
        return {"cmu": accion["cmu"], "resultado": "aplicado", "id": existente.id,
                "estado_previo": previo, "estado_nuevo": destino,
                "pdf_ya_en_drive": bool(accion.get("adjunto") and ya_tenia_pdf)}

    existente = FinanzasMandato.objects.filter(
        proyecto=accion["proyecto"], tercero=accion["tercero"],
        periodo=accion["periodo"], tipo=accion["tipo"],
    ).first()
    previo = existente.estado if existente else None
    destino = accion["estado"]
    # `sin_firma` solo estampa `fecha_envio`: `upsert` no toca el estado en esa
    # rama. Registrar un envío nunca puede degradar nada, así que no pasa por la
    # validación de transiciones — si no, un mandato que ya volvió firmado
    # rechazaría el registro de su propio envío.
    if destino == "sin_firma":
        pass
    elif existente and previo == destino:
        return {"cmu": accion["cmu"], "resultado": "sin_cambio", "estado": destino,
                "id": existente.id}
    elif existente and not transicion_firma_valida(previo, destino):
        return {"cmu": accion["cmu"], "resultado": "transicion_invalida",
                "estado_previo": previo, "estado_destino": destino}

    ya_tenia_pdf = bool(existente and existente.drive_url)
    drive_id, drive_url = _subir_si_falta(accion, correo, ya_tenia_pdf,
                                          accion["periodo"])

    m, creado = upsert(
        proyecto=accion["proyecto"], tercero=accion["tercero"],
        periodo=accion["periodo"], tipo=accion["tipo"], cmu=accion["cmu"],
        estado=destino, comentario=accion.get("comentario"),
        fecha=correo.fecha.date(), correo_ref=correo.message_id,
        drive_file_id=drive_id, drive_url=drive_url)
    # El estado que se REPORTA es el que quedó, no el que se pidió. Con
    # `sin_firma`, `upsert` solo estampa `fecha_envio` y deja el estado quieto;
    # reportar el destino hacía que la bitácora dijera
    # "enviado_inversionista → sin_firma" sobre un mandato que no se movió. Un
    # registro que miente sobre lo que pasó es peor que no tenerlo.
    return {"cmu": accion["cmu"], "resultado": "aplicado", "id": m.id,
            "pdf_ya_en_drive": ya_tenia_pdf, "creado": creado,
            "estado_previo": previo, "estado_nuevo": m.estado,
            "solo_fecha_envio": destino == "sin_firma" and m.estado != "sin_firma"}


def _subir_si_falta(accion: dict, correo: CorreoCrudo, ya_tenia_pdf: bool,
                    periodo) -> tuple[str | None, str | None]:
    """Sube el PDF a Drive, salvo que la fila ya tenga uno.

    **No re-subir lo que ya está.** `subir_pdf` siempre hace `files().create()`:
    no deduplica ni por nombre ni por contenido. Sin esta guarda, la primera
    corrida —que barre 30 días hacia atrás, ~93 correos— volvería a subir todos
    los PDF que el script de Jessica ya subió, dejando duplicados en Drive y
    apuntando cada fila a la copia nueva en vez de la original. Limpiar eso
    después es trabajo manual archivo por archivo.

    Reemplazar un PDF ya guardado (una reexpedición) se hace explícitamente, no
    como efecto colateral de reprocesar un correo viejo.
    """
    if not accion.get("adjunto") or ya_tenia_pdf:
        return None, None
    contenido = dict(expandir_adjuntos(list(correo.adjuntos))).get(accion["adjunto"])
    if not contenido:
        return None, None
    from apps.mandatos.services.drive import subir_pdf

    sub = f"{periodo.strftime('%Y-%m')}-{accion['tipo']}"
    res = subir_pdf(contenido, accion["adjunto"], sub)
    return res["id"], res["url"]


def procesar_correo(correo: CorreoCrudo, fuente: str):
    """Procesa un correo y devuelve su fila de bitácora, sin guardarla."""
    from apps.mandatos.models import MandatoCorreo

    d = decidir_finanzas(correo, fuente)
    registros = [_aplicar(a, correo) for a in d["acciones"]]
    aplicado = any(r["resultado"] == "aplicado" for r in registros)
    problema = any(r["resultado"] not in ("aplicado", "sin_cambio") for r in registros)
    return MandatoCorreo(
        message_id=correo.message_id, fecha=correo.fecha,
        remitente=(correo.remitente or "")[:255],
        asunto=(correo.asunto or "")[:1000],
        # La fuente EFECTIVA, no la de la pasada: un correo de Jessica hacia la
        # revisoría se reclasifica dentro de `decidir_finanzas`, y guardar la de
        # la pasada hacía que la bitácora dijera `envio_inversionista` sobre un
        # correo tratado como envío a revisión. Mentira útil para nadie.
        fuente=d["fuente_efectiva"],
        clasificacion=d["clasificacion"],
        resultado="aplicado" if aplicado else "omitido",
        requiere_revision=d["requiere_revision"] or problema,
        detalle={"acciones": registros, "sin_identidad": d["sin_identidad"],
                 "ignorados": d["ignorados"]})


REMITENTE_REVISORIA = "vlondono@jbp.com.co"
REMITENTE_ENVIO = "jessica@unergy.io"


def _pasadas() -> list[tuple]:
    """Las pasadas a hacer por cada buzón configurado, o `[]` si no hay ninguno.

    Tres por buzón: lo que llega de la revisoría (INBOX), lo que Jessica manda a
    inversionistas (INBOX, va en copia) y lo que sale hacia la revisoría
    (Enviados). La tercera es la que permite saber cuántos se enviaron, sin lo
    cual la reconciliación no puede detectar los que nunca volvieron.

    Parte del correo no pasa por adhara@: Jessica manda algunos desde su propia
    cuenta y esos viven en SU carpeta de Enviados. Los que están en los dos
    buzones se procesan UNA vez — la deduplicación va por Message-ID, que es el
    mismo mensaje mirado desde dos lados, no dos mensajes.
    """
    import imaplib
    import os

    from apps.mandatos.services.imap_client import buzones, carpeta_enviados

    credenciales = buzones()
    if not credenciales:
        return []

    pasadas = []
    for usuario, password in credenciales:
        pasadas.append((REMITENTE_REVISORIA, FUENTE_REVISORIA, "INBOX", "FROM",
                        usuario, password))
        pasadas.append((REMITENTE_ENVIO, FUENTE_ENVIO, "INBOX", "FROM",
                        usuario, password))

        # La carpeta de Enviados hay que preguntarla a cada servidor: su nombre
        # depende del idioma de la cuenta, por eso se busca por la bandera \Sent.
        try:
            imap = imaplib.IMAP4_SSL(
                os.environ.get("IMAP_HOST", "imap.gmail.com"),
                int(os.environ.get("IMAP_PORT", "993")),
            )
            imap.login(usuario, password)
            enviados = carpeta_enviados(imap)
            imap.logout()
        except Exception as exc:
            logger.error("no se pudo consultar Enviados de %s: %s", usuario, exc)
            enviados = None

        if enviados:
            pasadas.append((REMITENTE_REVISORIA, FUENTE_SALIENTE, enviados, "TO",
                            usuario, password))
        else:
            logger.warning("sin carpeta de Enviados en %s — no se registrarán los "
                           "envíos hechos desde ese buzón", usuario)
    return pasadas


def revisar_correos(dias: int = 30) -> dict:
    """Punto de entrada de la tarea. Nunca lanza hacia el worker.

    Devuelve un resumen además de loguearlo, para que el endpoint manual pueda
    reportar qué pasó sin obligar a ir a buscar en los logs.

    `dias` es la ventana hacia atrás. 30 alcanza para la operación diaria; para
    recuperar histórico se sube (730 son dos años). Una ventana grande es cara
    —cada PDF se abre para revisar firmas— pero es segura de reintentar: la
    deduplicación por Message-ID hace que una corrida interrumpida retome donde
    quedó en vez de rehacer lo ya hecho.

    **Transacción por correo**: uno que reviente no arrastra a los demás.
    """
    from django.db import transaction

    from apps.mandatos.models import MandatoCorreo
    from apps.mandatos.services.imap_client import buscar_correos

    pasadas = _pasadas()
    if not pasadas:
        logger.info("credenciales de correo no configuradas, se omite")
        return {"ok": False, "motivo": "credenciales no configuradas"}

    vistos = set(MandatoCorreo.objects.values_list("message_id", flat=True))
    nuevos = 0
    resumen: dict = {"aplicado": 0, "omitido": 0, "error": 0}
    para_revisar: list[int] = []

    for direccion, fuente, carpeta, campo, usuario, password in pasadas:
        for correo in buscar_correos(direccion, dias=dias, carpeta=carpeta,
                                     campo=campo, usuario=usuario,
                                     password=password):
            if correo.message_id in vistos:
                continue
            try:
                with transaction.atomic():
                    fila = procesar_correo(correo, fuente)
                    fila.save()
                vistos.add(correo.message_id)
                nuevos += 1
                resumen[fila.resultado] = resumen.get(fila.resultado, 0) + 1
                if fila.requiere_revision:
                    para_revisar.append(fila.id)
                logger.info("%s — %s/%s, %d acciones", correo.message_id,
                            fila.clasificacion, fila.resultado,
                            len(fila.detalle.get("acciones", [])))
            except Exception as exc:
                logger.error("fallo en %s: %s", correo.message_id, exc)
                try:
                    with transaction.atomic():
                        MandatoCorreo.objects.create(
                            message_id=correo.message_id, fecha=correo.fecha,
                            remitente=(correo.remitente or "")[:255],
                            asunto=(correo.asunto or "")[:1000], fuente=fuente,
                            clasificacion="desconocido", resultado="error",
                            requiere_revision=True, detalle={"error": str(exc)})
                    vistos.add(correo.message_id)
                    resumen["error"] += 1
                except Exception:
                    logger.exception("tampoco se pudo registrar el fallo")

    logger.info("corrida terminada, %d correos nuevos — %s", nuevos, resumen)
    return {"ok": True, "correos_nuevos": nuevos, "por_resultado": resumen,
            "requieren_revision": para_revisar,
            "pasadas": len(pasadas), "dias": dias}


# ── Corrida en segundo plano ─────────────────────────────────────────────────
#
# Leer 90 días son minutos de trabajo: cada PDF se abre para revisar firmas y
# los mandatos nuevos se suben a Drive. El proxy que hay delante del backend
# corta la conexión mucho antes y devuelve un 502, que parece un fallo cuando en
# realidad el proceso sigue vivo. Por eso el endpoint responde de inmediato.
_EN_CURSO = False


def ingesta_en_curso() -> bool:
    """Si hay una corrida andando ahora mismo.

    Evita que dos se pisen. La deduplicación por Message-ID hace que dos pasadas
    simultáneas sean casi inofensivas, pero podrían tomar el mismo correo a la
    vez y duplicar subidas a Drive — que es justo lo que no se puede deshacer sin
    trabajo manual.

    `ponytail: bandera de módulo`. Vale con un solo proceso, igual que el estado
    del motor de alarmas. Con varios workers esto pasa a un lock en Redis.
    """
    return _EN_CURSO


def revisar_correos_async(dias: int = 30) -> None:
    """Igual que `revisar_correos`, para lanzar en un hilo o desde la tarea.

    No devuelve nada porque nadie está escuchando: el resultado se consulta
    después en la bitácora. Nunca lanza, para que un fallo no quede como una
    excepción huérfana en el log sin contexto.
    """
    global _EN_CURSO
    if _EN_CURSO:
        logger.warning("ya hay una corrida en curso, se omite")
        return
    _EN_CURSO = True
    try:
        resultado = revisar_correos(dias=dias)
        logger.info("corrida en segundo plano terminada — %s", resultado)
    except Exception as exc:
        logger.error("la corrida en segundo plano falló: %s", exc)
    finally:
        _EN_CURSO = False
