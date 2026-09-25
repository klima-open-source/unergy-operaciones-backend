"""URLconf raiz. Un solo include: toda la API cuelga de `api/`.

Las rutas deben quedar IDENTICAS a las que sirve FastAPI hoy
(`/api/v1/<recurso>`), porque el frontend en produccion las llama tal cual.
La cadena es la misma que describe migration.md, con `v1` en vez de `v2`:

    config/urls.py -> api/urls.py -> api/v1/urls.py -> api/v1/<recurso>/urls.py
"""

from django.http import JsonResponse
from django.urls import include, path
from django.contrib import admin


# Nombre de la app en `/health`. Es el mismo literal que el default de
# `APP_NAME` en `app/core/config.py`: el healthcheck del compose compara el
# status, pero el front lo muestra, así que la respuesta no puede cambiar.
APP_NAME = "Plataforma Operaciones Unergy"


def health(request):
    """Sin autenticación ni base: solo dice que el proceso responde."""
    return JsonResponse({"status": "ok", "app": APP_NAME})


health.metodos_http = ["GET"]   # lo lee tests/test_paridad_urls.py

urlpatterns = [
    path("api/", include("api.urls")),
    path("health", health),
    path("admin/", admin.site.urls),
]
