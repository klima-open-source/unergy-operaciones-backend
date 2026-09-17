"""Endpoint de diagnóstico: ¿este servidor alcanza el FTP de XM?

Temporal, solo-admin. Responde si la Descarga de XM podría vivir en el backend
sin el agente local — la única incógnita es de red (si el servidor tiene ruta al
FTP de XM). NO descarga nada ni recibe/guarda credenciales: solo prueba la
conexión y devuelve hasta qué etapa llegó.

    GET /api/v1/descarga-xm/probar-ftp
"""
from rest_framework.response import Response
from rest_framework.views import APIView

from api.permissions import RolePermission
from apps.mercado_xm.services.probar_ftp import probar_conexion_ftp


class ProbarFtpXmView(APIView):
    permission_classes = [RolePermission]
    required_role = ["admin"]

    def get(self, request):
        return Response(probar_conexion_ftp(timeout=15))
