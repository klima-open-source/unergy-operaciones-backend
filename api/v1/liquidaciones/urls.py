"""Rutas de liquidaciones.

`resumen-panel`, `resumen-panel-rango` y `catalogos/tipos` son
acciones de LISTA, y `DefaultRouter` las registra antes que la ruta de detalle;
`tests/test_resolucion_rutas.py` lo verifica.
"""

from django.urls import include, path
from rest_framework import routers

from . import views

router = routers.DefaultRouter(trailing_slash=False)
router.register(
    "liquidaciones", views.LiquidacionViewSet, basename="liquidaciones"
)

urlpatterns = [path("", include(router.urls))]
