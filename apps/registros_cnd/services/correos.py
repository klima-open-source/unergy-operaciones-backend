"""Generacion de borradores de correo tipo (OR / XM / Solenium).

Portado de references/plantillas-correos.md del skill seguimiento-conexion-xm. Rellena
las variables con datos del proyecto/registro; deja [corchetes] cuando el dato falta
(nunca inventar). Devuelve un borrador {tipo, asunto, para, cc, cuerpo, adjuntos} para
que el usuario lo revise y envie desde su buzon.
"""

from __future__ import annotations


TIPOS_CORREO = ("SOLICITUD_FIRMAS_OR", "CREACION_MDC_XM", "DOC_FRONTERA_SOLENIUM")

CC_UNERGY = "operaciones@unergy.io"


def _ph(valor, marcador: str) -> str:
    """Devuelve el valor si existe; si no, un marcador entre corchetes."""
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        return f"[{marcador}]"
    return str(valor)


def _nombre_or(proyecto) -> str:
    op = getattr(proyecto, "operador", None)
    if op is not None:
        return op.nombre_comercial or op.nombre_legal
    return "[OR]"


def _correos_or(proyecto) -> list[str]:
    op = getattr(proyecto, "operador", None)
    if op is None:
        return []
    return [c.email for c in getattr(op, "contactos", []) if getattr(c, "activo", True) and c.email]


def _capacidad_mw(proyecto) -> str:
    # 'capacidad_efectiva_neta_mw' era un fallback intermedio a un campo que
    # nunca existió en Proyecto (getattr(..., None) siempre caía a None) --
    # eliminado, auditoría de Proyectos 2026-08-27. potencia_con_cen_mw
    # sigue en 0% de los proyectos hoy (dato regulatorio que nadie ha
    # cargado), así que en la práctica esta función siempre resuelve por
    # potencia_ac_kw -- que desde el fix del 2026-08-19 sí es
    # potencia AC confiable, no un valor arbitrario.
    v = getattr(proyecto, "potencia_con_cen_mw", None)
    if v:
        return f"{float(v):.3f}"
    kwp = getattr(proyecto, "potencia_ac_kw", None)
    if kwp:
        return f"{float(kwp) / 1000:.3f}"
    return "[X.XXX]"


def _nombre_mdc(proyecto) -> str:
    """Nombre a usar identico en cartas/MDC (p. ej. 'MGS 0040 - La Cacica')."""
    codigo = getattr(proyecto, "codigo_cnd", None)
    nombre = proyecto.nombre_comercial
    if codigo and codigo not in (nombre or ""):
        return f"{codigo} - {nombre}"
    return nombre


def generar_correo(registro, tipo: str) -> dict:
    if tipo not in TIPOS_CORREO:
        raise ValueError(f"Tipo de correo desconocido: {tipo}. Validos: {', '.join(TIPOS_CORREO)}")

    proyecto = registro.proyecto
    nombre = proyecto.nombre_comercial
    nombre_mdc = _nombre_mdc(proyecto)
    or_nombre = _nombre_or(proyecto)

    if tipo == "SOLICITUD_FIRMAS_OR":
        asunto = "Unergy: solicitud de firmas para las cartas en el aplicativo MDC de XM."
        cuerpo = (
            f"Cordial saludo, respetado equipo de {or_nombre}. Esperamos que se encuentren muy bien.\n\n"
            f"En calidad de representante de frontera del proyecto {nombre}, amablemente "
            f"solicitamos las firmas de las cartas 9.1 y 9.7, que se anexan a este correo junto con "
            f"la factibilidad y prorroga, con el fin de avanzar en el proceso de registro del proyecto "
            f"{nombre} identificado en el sistema con ID No. {_ph(registro.id_requerimiento_or, 'id_requerimiento_or')} "
            f"y Radicado No.: {_ph(registro.numero_expediente, 'radicado')}.\n\n"
            f"Agradecemos de antemano su colaboracion y quedamos atentos a su pronta respuesta."
        )
        return {
            "tipo": tipo,
            "asunto": asunto,
            "para": _correos_or(proyecto),
            "cc": [CC_UNERGY, "tramites@solenium.co"],
            "cuerpo": cuerpo,
            "adjuntos": ["Formato CND-9.1", "Formato CND-9.7", "Factibilidad (CREG 174)", "Prorroga (si existe)"],
        }

    if tipo == "CREACION_MDC_XM":
        fpo = getattr(proyecto, "fecha_entrada_operacion", None)
        asunto = f"Unergy: Solicitud de creacion en el aplicativo del proyecto {nombre_mdc}."
        cuerpo = (
            "Cordial saludo, respetado equipo de XM. Esperamos que se encuentren bien.\n\n"
            f"Unergy, en su calidad de representante del proyecto de generacion distribuida denominado "
            f"{nombre_mdc}, con una capacidad efectiva neta de {_capacidad_mw(proyecto)} MW, solicita la "
            "creacion del proyecto en el aplicativo MDC bajo el dominio de UNERGY ENERGIA DIGITAL S.A.S ESP.\n\n"
            "Para lo anterior se relaciona la siguiente informacion:\n"
            f"- Nombre del proyecto: {nombre_mdc}\n"
            "- Tipo de generacion: Generador Distribuido\n"
            "- Tipo de tecnologia a utilizar: Solar FV\n"
            f"- Capacidad efectiva neta (MW): {_capacidad_mw(proyecto)}\n"
            f"- Punto de conexion asignado: {_ph(registro.punto_conexion_texto, 'punto de conexion del ambito')}\n"
            "- Cuenta con almacenamiento de energia: [No]\n"
            f"- Fecha de Puesta en Operacion: {_ph(fpo, 'dd de mes de aaaa')}\n\n"
            "Se anexa la carta 9.1 firmada por el OR.\n"
            "Quedamos atentos a su respuesta, de antemano muchas gracias."
        )
        return {
            "tipo": tipo,
            "asunto": asunto,
            "para": ["info@xm.com.co"],
            "cc": [CC_UNERGY, "tramites@solenium.co"],
            "cuerpo": cuerpo,
            "adjuntos": ["Carta 9.1 firmada por el OR (PDF)"],
        }

    # DOC_FRONTERA_SOLENIUM
    asunto = f"Unergy: Documentacion para el Registro de Frontera {nombre_mdc}."
    cuerpo = (
        "Cordial saludo, espero que te encuentres muy bien.\n\n"
        f"Por este medio te solicito amablemente la documentacion requerida para el registro de la "
        f"frontera del proyecto {nombre_mdc}. Los documentos solicitados son:\n\n"
        "1. Certificados de conformidad: medidores, TC, TP, cables de control, celda, bloque de pruebas.\n"
        "2. Certificados de calibracion: medidores, TC y TP.\n"
        "3. Prueba de rutina de trafos (si la calibracion de CTs y PTs tiene mas de 6 meses).\n"
        "4. Diagrama unifilar y planos del sistema de medida.\n"
        "5. Memorias de calculo del sistema de medida (burden de frontera, caida de tension en el cable de los TPs).\n"
        "6. Documentacion tecnica: datasheet y manual de TP, CT, medidor y modem.\n"
        "7. Consumo y generacion: Excel con proyeccion mensual y diaria, y transferencia maxima horaria.\n"
        "8. Matricula profesional del personal que instalara la frontera / ingeniero de proyecto.\n"
        "9. Fotos de los equipos (TP, TC, medidores, celda, cables, bloque de pruebas), placas y seriales.\n"
        "10. Certificados de compra: medidores, TC y PTs, celda, cable de control, bloque de pruebas.\n\n"
        "Importante: verificar que la documentacion coincida con los equipos comprados y que esten en sitio."
    )
    return {
        "tipo": tipo,
        "asunto": asunto,
        "para": ["tramites@solenium.co"],
        "cc": [CC_UNERGY],
        "cuerpo": cuerpo,
        "adjuntos": [],
    }
