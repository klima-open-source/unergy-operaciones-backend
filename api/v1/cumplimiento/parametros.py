"""Los `Query(..., ge=, le=)` de FastAPI, traducidos.

FastAPI valida el query string contra la firma del endpoint y devuelve **422**
cuando `year=1999`. DRF no valida query params: sin esto, un año fuera de rango
llegaría hasta la consulta y devolvería una lista vacía con 200, que es peor que
un error — parece un mes sin datos.
"""

from api.exceptions import NoProcesable


def entero(request, nombre, defecto=None, minimo=None, maximo=None, requerido=False):
    crudo = request.query_params.get(nombre)
    if crudo in (None, ""):
        if requerido:
            raise NoProcesable(f"Falta el parámetro obligatorio '{nombre}'")
        return defecto
    try:
        valor = int(crudo)
    except (TypeError, ValueError):
        raise NoProcesable(f"'{nombre}' debe ser un número entero")
    if minimo is not None and valor < minimo:
        raise NoProcesable(f"'{nombre}' debe ser >= {minimo}")
    if maximo is not None and valor > maximo:
        raise NoProcesable(f"'{nombre}' debe ser <= {maximo}")
    return valor


def entero_lista(request, nombre):
    """`?ppa_id=1&ppa_id=2` -> `[1, 2]`. Igual que `entero`, para query params
    repetidos: un valor no numérico da 422 en vez de un ValueError sin capturar."""
    valores = []
    for crudo in request.query_params.getlist(nombre):
        try:
            valores.append(int(crudo))
        except (TypeError, ValueError):
            raise NoProcesable(f"'{nombre}' debe ser un número entero")
    return valores or None


def bandera(request, nombre, defecto=False):
    """`?incluir_todos=true`. FastAPI acepta true/false/1/0/yes/on."""
    crudo = request.query_params.get(nombre)
    if crudo is None:
        return defecto
    return crudo.strip().lower() in ("1", "true", "yes", "on", "t", "y")


def anio(request, requerido=True, defecto=None):
    return entero(request, "year", defecto, 2020, 2050, requerido)


def mes(request, requerido=True, defecto=None):
    return entero(request, "month", defecto, 1, 12, requerido)
