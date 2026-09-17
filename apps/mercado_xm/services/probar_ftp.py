"""Prueba si el servidor alcanza el FTP de XM (xmftps.xm.com.co:210).

Lógica compartida por el management command `probar_ftp_xm` y el endpoint de
diagnóstico. NO descarga nada ni guarda credenciales: solo verifica hasta qué
etapa de la conexión llega. El TCP connect es la etapa que decide — si abre, el
servidor tiene ruta al FTP y la Descarga de XM puede vivir en el backend sin el
agente local.
"""
import ftplib
import socket
import ssl

HOST_XM = "xmftps.xm.com.co"
PUERTO_XM = 210

_VEREDICTO_SI = (
    "Hay ruta de red al FTP de XM. La Descarga de XM PUEDE vivir en el backend; "
    "el agente local se puede jubilar."
)
_VEREDICTO_NO = (
    "El servidor NO alcanza el FTP de XM. La Descarga de XM no puede vivir en el "
    "backend hasta que XM autorice la IP de este servidor (o se ponga tras la "
    "misma VPN). El agente local se queda."
)


def _cerrar(ftp):
    try:
        ftp.quit()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass


def probar_conexion_ftp(host=HOST_XM, puerto=PUERTO_XM, timeout=15,
                         usuario=None, clave=None) -> dict:
    """Devuelve un dict con el resultado de la prueba:

    - alcanza (bool): si el TCP connect abrió (el hecho que decide).
    - etapa (str): hasta dónde llegó — 'ninguna' | 'tcp' | 'tls' | 'login'.
    - login_real (bool): si se probó con credenciales reales.
    - detalle (str): mensaje humano de la última etapa.
    - veredicto (str): frase resumen de la decisión.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ftp = ftplib.FTP_TLS(context=ctx)

    # Etapa 1: TCP — la que decide.
    try:
        ftp.connect(host, puerto, timeout=timeout)
    except (socket.timeout, TimeoutError):
        return {"alcanza": False, "etapa": "ninguna", "login_real": bool(usuario),
                "detalle": f"Timeout en el TCP connect a {host}:{puerto}.",
                "veredicto": _VEREDICTO_NO}
    except OSError as e:
        return {"alcanza": False, "etapa": "ninguna", "login_real": bool(usuario),
                "detalle": f"No se pudo conectar a {host}:{puerto}: {e}",
                "veredicto": _VEREDICTO_NO}

    # A partir de aquí la ruta de red existe (alcanza=True). Las siguientes
    # etapas son confirmación adicional, no cambian el veredicto.
    resultado = {"alcanza": True, "etapa": "tcp", "login_real": bool(usuario),
                 "detalle": f"TCP connect OK a {host}:{puerto}.",
                 "veredicto": _VEREDICTO_SI}

    try:
        ftp.auth()
        resultado["etapa"] = "tls"
        resultado["detalle"] = "TCP + AUTH/TLS OK."
    except Exception as e:
        resultado["detalle"] = f"TCP OK pero AUTH/TLS falló: {e}"
        _cerrar(ftp)
        return resultado

    usuario_p = usuario or "prueba_conectividad"
    clave_p = clave or "prueba"
    try:
        ftp.login(user=usuario_p, passwd=clave_p)
        ftp.prot_p()
        resultado["etapa"] = "login"
        resultado["detalle"] = f"TCP + TLS + LOGIN OK con '{usuario_p}'."
    except ftplib.error_perm as e:
        resultado["etapa"] = "login"
        if usuario:
            resultado["detalle"] = (
                f"La red llega, pero el login con '{usuario}' fue rechazado: {e} "
                "(revisar usuario/clave).")
        else:
            resultado["detalle"] = (
                f"Llegó hasta el login (rechazo esperado con credenciales de prueba: {e}).")
    finally:
        _cerrar(ftp)

    return resultado
