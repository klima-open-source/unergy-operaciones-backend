from django.urls import path

from . import views

urlpatterns = [
    path("descarga-xm/probar-ftp", views.ProbarFtpXmView.as_view(), name="descarga-xm-probar-ftp"),
]
