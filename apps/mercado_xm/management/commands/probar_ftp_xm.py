"""Prueba si ESTE servidor puede alcanzar el FTP de XM (xmftps.xm.com.co:210).

El feature "Descarga de XM" hoy vive en un agente local en el PC de quien lo
usa, porque el FTP de XM solo acepta conexiones desde IPs autorizadas y la nube
vieja (Railway) no estaba en la lista. El backend nuevo corre en un servidor
propio de Unergy; si ESE servidor sí alcanza el FTP, la descarga puede vivir en
el backend y el agente local sobra. Esa es la única incógnita, y es de red.

Este comando NO descarga nada ni necesita credenciales reales. La lógica vive en
`apps/mercado_xm/services/probar_ftp.py` (la comparte el endpoint de diagnóstico).

En el servidor:

    docker compose exec operaciones python manage.py probar_ftp_xm

Opcional, para probar además un login real (sin guardar nada):

    docker compose exec operaciones python manage.py probar_ftp_xm --usuario X --clave Y
"""
from django.core.management.base import BaseCommand

from apps.mercado_xm.services.probar_ftp import HOST_XM, PUERTO_XM, probar_conexion_ftp


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
        w = self.stdout.write
        w(f"Probando conexion a {opts['host']}:{opts['puerto']} (timeout {opts['timeout']}s)...")

        r = probar_conexion_ftp(
            host=opts["host"], puerto=opts["puerto"], timeout=opts["timeout"],
            usuario=opts["usuario"], clave=opts["clave"],
        )

        marca = "[OK]" if r["alcanza"] else "[X]"
        estilo = self.style.SUCCESS if r["alcanza"] else self.style.ERROR
        w(estilo(f"{marca} etapa alcanzada: {r['etapa']} - {r['detalle']}"))
        w(estilo(f"VEREDICTO: {r['veredicto']}"))
