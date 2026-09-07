"""Rutas de la v1 — un `include()` por recurso, sin logica.

El prefijo de cada recurso vive en su propio `urls.py` (no aca) para que las
rutas queden exactamente donde FastAPI las sirve hoy. Se agrega una linea por
modulo portado; mientras tanto ese prefijo lo sigue atendiendo FastAPI.
"""

from django.urls import include, path

urlpatterns = [
    path("", include("api.v1.alertas.urls")),
    path("", include("api.v1.auth.urls")),
    path("", include("api.v1.arriendos.urls")),
    path("", include("api.v1.asic.urls")),
    path("", include("api.v1.api_keys.urls")),
    path("", include("api.v1.clasificacion_energia.urls")),
    path("", include("api.v1.clientes.urls")),
    path("", include("api.v1.comercial.urls")),
    path("", include("api.v1.contratos_servicio.urls")),
    path("", include("api.v1.cumplimiento.urls")),
    path("", include("api.v1.dashboard.urls")),
    path("", include("api.v1.estados_resultados.urls")),
    path("", include("api.v1.evo_proxy.urls")),
    path("", include("api.v1.facturacion.urls")),
    path("", include("api.v1.fallas.urls")),
    path("", include("api.v1.finanzas_mandatos.urls")),
    path("", include("api.v1.fronteras.urls")),
    path("", include("api.v1.garantias_ajustes.urls")),
    path("", include("api.v1.garantias_modelo.urls")),
    path("", include("api.v1.garantias_proyecciones.urls")),
    path("", include("api.v1.generacion.urls")),
    path("", include("api.v1.generacion_solar.urls")),
    path("", include("api.v1.informe_om.urls")),
    path("", include("api.v1.informes.urls")),
    path("", include("api.v1.liquidaciones.urls")),
    path("", include("api.v1.liquidaciones_proxy.urls")),
    path("", include("api.v1.mandatos.urls")),
    path("", include("api.v1.mapa.urls")),
    path("", include("api.v1.monitoreo.urls")),
    path("", include("api.v1.notificaciones.urls")),
    path("", include("api.v1.operadores_red.urls")),
    path("", include("api.v1.om.urls")),
    path("", include("api.v1.panel_contable.urls")),
    path("", include("api.v1.polizas.urls")),
    path("", include("api.v1.ppa.urls")),
    path("", include("api.v1.solar.urls")),
    path("", include("api.v1.starlink.urls")),
    path("", include("api.v1.verificacion_costos.urls")),
    path("", include("api.v1.portafolios.urls")),
    path("", include("api.v1.proximos_energizar.urls")),
    path("", include("api.v1.proyectos.urls")),
    path("", include("api.v1.reconectadores.urls")),
    path("", include("api.v1.registros_cnd.urls")),
    path("", include("api.v1.reporte_cgm.urls")),
    path("", include("api.v1.reporte_energia.urls")),
    path("", include("api.v1.retos.urls")),
]
