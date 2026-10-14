"""Rutas de PPA.

`partes`, `resumen-global` e `ipp/mensual` son acciones de
LISTA, y `DefaultRouter` las registra antes de la ruta de detalle: el `{pk}` no
se las come. `tests/test_resolucion_rutas.py` lo verifica.
"""

from django.urls import include, path
from rest_framework import routers

from . import views

router = routers.DefaultRouter(trailing_slash=False)
router.register("ppa", views.PpaViewSet, basename="ppa")

urlpatterns = [path("", include(router.urls))]
