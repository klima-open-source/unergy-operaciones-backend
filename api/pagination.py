"""Paginacion base — el contrato de respuesta que ya consume el frontend.

FastAPI devuelve hoy `{items, total, page, size}` en los listados paginados. DRF
devuelve `{count, next, previous, results}`. Mantener el contrato es parte de
"los mismos endpoints": un cambio de nombres de clave rompe el frontend igual
que un cambio de ruta.
"""

from collections import OrderedDict

from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

# El tope de filas por pagina de TODA la API. Era 500 (y 5000 en fallas), y
# `/api/v1/fallas?size=500` mataba al Worker de Cloudflare que sirve
# operaciones.unergy.io con un 1102 (exceeded resource limits): 500 filas del
# serializer de lista son varios MB. `GZipMiddleware` bajo el cuerpo; el tope
# lo acota.
TOPE_FILAS = 100


def recortar(tamano: int) -> int:
    """Recorta a `TOPE_FILAS`, sin fallar.

    `BasePagination` ya lo hace solo (DRF pasa `max_page_size` como `cutoff` a
    `_positive_int`), pero media docena de listados paginan a mano con su propio
    `_entero`. Recortar y no lanzar 422 es deliberado: el frontend pide 500 hoy,
    y un 422 lo dejaria sin listado en vez de con las primeras 100 filas.
    """
    return min(tamano, TOPE_FILAS)


class BasePagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "size"
    page_query_param = "page"
    # 100 y no 500: `/api/v1/fallas?size=500` mataba al Worker de Cloudflare que
    # sirve operaciones.unergy.io con un 1102 (exceeded resource limits) — 500
    # filas del serializer de lista son varios MB. `GZipMiddleware`
    # (`config/settings.py`) bajo el cuerpo pero no alcanzo; el tope si.
    #
    # DRF RECORTA, no falla: `get_page_size` pasa `max_page_size` como `cutoff`
    # a `_positive_int`, asi que `?size=500` devuelve 100 filas con un 200. Eso
    # es deliberado — un 422 dejaria al frontend sin listado.
    max_page_size = TOPE_FILAS

    def get_paginated_response(self, data):
        return Response(
            OrderedDict(
                [
                    ("items", data),
                    ("total", self.page.paginator.count),
                    ("page", self.page.number),
                    ("size", self.get_page_size(self.request)),
                ]
            )
        )


class PaginacionConPaginas(BasePagination):
    """`{items, total, page, size, pages}` — el `PaginatedResponse` de Pydantic.

    Los listados que FastAPI declara con `response_model=PaginatedResponse[X]`
    incluyen `pages`; los que devuelven el dict a mano, no. Son dos contratos
    distintos que ya están en producción, así que se conservan los dos.
    """

    def get_paginated_response(self, data):
        respuesta = super().get_paginated_response(data)
        total = self.page.paginator.count
        tamano = self.get_page_size(self.request)
        respuesta.data["pages"] = -(-total // tamano) if tamano else 0
        return respuesta
