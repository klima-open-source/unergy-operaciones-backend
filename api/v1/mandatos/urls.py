"""Rutas de la lectura del correo de mandatos (`/api/v1/mandatos/...`)."""

from django.urls import include, path
from rest_framework import routers

from . import views

router = routers.DefaultRouter(trailing_slash=False)
router.register("mandatos", views.MandatoViewSet, basename="mandatos")

urlpatterns = [path("", include(router.urls))]
