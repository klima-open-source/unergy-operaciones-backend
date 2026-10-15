"""Rutas de patrimonios autónomos."""

from django.urls import include, path
from rest_framework import routers

from . import views

router = routers.DefaultRouter(trailing_slash=False)
router.register("patrimonios", views.PatrimonioViewSet, basename="patrimonios")

urlpatterns = [path("", include(router.urls))]
