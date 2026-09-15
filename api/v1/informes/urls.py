"""Rutas de informes.

Registra el viewset dos veces: sin barra (`trailing_slash=False`) para
compatibilidad con tests y clientes existentes, y con barra (`trailing_slash=True`)
porque el frontend los llama con barra final.
"""

from django.urls import include, path
from rest_framework import routers

from . import views

router = routers.DefaultRouter(trailing_slash=False)
router.register("informes", views.InformeViewSet, basename="informes")

router_slash = routers.DefaultRouter(trailing_slash=True)
router_slash.register("informes", views.InformeViewSet, basename="informes")

urlpatterns = [
    path("", include(router.urls)),
    path("", include(router_slash.urls)),
]
