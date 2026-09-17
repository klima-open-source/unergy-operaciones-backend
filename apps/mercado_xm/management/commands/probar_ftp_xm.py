"""Prueba si ESTE servidor puede alcanzar el FTP de XM (xmftps.xm.com.co:210).

El feature "Descarga de XM" hoy vive en un agente local en el PC de quien lo
usa, porque el FTP de XM solo acepta conexiones desde IPs autorizadas y la nube
vieja (Railway) no estaba en la lista. El backend nuevo corre en un servidor
propio de Unergy; si ESE servidor sí alcanza el FTP, la descarga puede vivir en
el backend y el agente local sobra. Esa es la única incógnita, y es de red, no
de código.

Este comando NO descarga nada ni necesita credenciales reales: solo verifica
hasta qué etapa de la conexión llega. El TCP connect al puerto 210 es lo que
decide todo — si eso abre, el servidor tiene ruta al FTP.

En el servidor:

    docker compose exec operaciones python manage.py probar_ftp_xm

Opcional, para probar además un login real (sin guardar nada):

    docker compose exec operaciones python manage.py probar_ftp_xm --usuario X --clave Y
"""
import ftplib
import socket
import ssl

from django.core.management.base import BaseCommand

HOST_XM = "xmftps.xm.com.co"
PUERTO_XM = 210


def _cerrar(ftp):
    try:
        ftp.quit()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass


class Command(BaseCommand):
    help = (
        "Verifica si este servidor alcanza el FTP de XM, para saber si la "
        "Descarga de XM puede vivir en el backend sin el agente local."
    )

    def add_arguments(self, parser):
        parser.add_argument("--host", default=HOST_XM)
        parser.add_argument("--puerto", type=int, default=PUERTO_XM)
        parser.add_argument("--timeout", type=int, default=15)
        parser.add_argument("--usuario", default=None, help="Opcional: probar un login real.")
        parser.add_argument("--clave", default=None)

    def handle(self, *args, **opts):
        host, puerto, timeout = opts["host"], opts["puerto"], opts["timeout"]
        w = self.stdout.write

        w(f"Probando conexión a {host}:{puerto} (timeout {timeout}s)...")

        # SSL relajado: el servidor de XM no pasa verificación estricta. Igual
        # que el agente local que ya funciona en producción.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ftp = ftplib.FTP_TLS(context=ctx)

        # ── Etapa 1: TCP. LA etapa que decide. Si da timeout/rechazo, el
        # servidor NO tiene ruta al FTP (IP no autorizada / bloqueo de red).
        try:
            ftp.connect(host, puerto, timeout=timeout)
        except (socket.timeout, TimeoutError):
            w(self.style.ERROR("\n[X] TIMEOUT en el TCP connect."))
            w(self.style.ERROR(
                "VEREDICTO: el servidor NO alcanza el FTP de XM. La Descarga de XM NO puede "
                "vivir en el backend hasta que XM autorice la IP de este servidor (o se ponga "
                "tras la misma VPN). El agente local se queda."))
            return
        except OSError as e:
            w(self.style.ERROR(f"\n[X] No se pudo conectar (OSError): {e}"))
            w(self.style.ERROR(
                "VEREDICTO: sin ruta al FTP (conexión rechazada). Igual que un timeout para "
                "esta decisión: la descarga no puede vivir en el backend todavía."))
            return

        w(self.style.SUCCESS("[OK] TCP connect OK - el servidor SI alcanza el puerto 210 de XM."))
        w(self.style.SUCCESS(
            "VEREDICTO: hay ruta de red al FTP de XM. La Descarga de XM PUEDE vivir en el "
            "backend nuevo; el agente local se puede jubilar."))
        w("\n(Las siguientes etapas son confirmación adicional, no cambian el veredicto.)")

        # ── Etapa 2: TLS handshake. Confirma que del otro lado está el FTPS de XM.
        try:
            ftp.auth()
        except Exception as e:
            w(self.style.WARNING(f"[!] TCP abrió pero el AUTH/TLS falló: {e}"))
            w("  La ruta de red existe (que es lo que importa); reportar el detalle del TLS.")
            _cerrar(ftp)
            return
        w(self.style.SUCCESS("[OK] AUTH/TLS OK."))

        # ── Etapa 3: login. Sin credenciales reales esperamos un rechazo 530,
        # que ya prueba que la conexión llega hasta la autenticación.
        usuario = opts["usuario"] or "prueba_conectividad"
        clave = opts["clave"] or "prueba"
        login_real = bool(opts["usuario"])
        try:
            ftp.login(user=usuario, passwd=clave)
            ftp.prot_p()
            w(self.style.SUCCESS(f"[OK] LOGIN OK con '{usuario}' - el servidor alcanza y autentica contra XM."))
        except ftplib.error_perm as e:
            if login_real:
                w(self.style.WARNING(f"[!] El login con '{usuario}' fue rechazado: {e}"))
                w("  La RED sí llega (rechazó credenciales, no timeout). Revisar usuario/clave.")
            else:
                w(self.style.SUCCESS(f"[OK] Llegó hasta el login (rechazo esperado con credenciales de prueba: {e})."))
        finally:
            _cerrar(ftp)
