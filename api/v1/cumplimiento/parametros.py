"""Los `Query(..., ge=, le=)` de FastAPI, traducidos.

FastAPI valida el query string contra la firma del endpoint y devuelve **422**
cuando `year=1999`. DRF no valida query params: sin esto, un año fuera de rango
llegaría hasta la consulta y devolvería una lista vacía con 200, que es peor que
un error — parece un mes sin datos.
"""

from datetime import date

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


def bandera(request, nombre, defecto=False):
    """`?incluir_todos=true`. FastAPI acepta true/false/1/0/yes/on."""
    crudo = request.query_params.get(nombre)
    if crudo is None:
        return defecto
    return crudo.strip().lower() in ("1", "true", "yes", "on", "t", "y")


def fecha(request, nombre, defecto=None, requerido=False):
    """`?activa_en_fecha=2026-09-01`. FastAPI lo declaraba `date | None` y
    devolvia 422 solo con verlo en la firma.

    Sin esto la cadena cruda llega hasta el ORM y Django levanta un
    `django.core.exceptions.ValidationError`, que **no** es de DRF: su
    `EXCEPTION_HANDLER` no lo traduce y sale un **500**. Es peor que un error de
    validacion, porque parece una caida del servidor.
    """
    crudo = request.query_params.get(nombre)
    if crudo in (None, ""):
        if requerido:
            raise NoProcesable(f"Falta el parámetro obligatorio '{nombre}'")
        return defecto
    try:
        return date.fromisoformat(crudo)
    except (TypeError, ValueError):
        raise NoProcesable(
            f"'{nombre}' no es una fecha válida ('{crudo}'). Usá el formato YYYY-MM-DD."
        )


def anio(request, requerido=True, defecto=None):
    return entero(request, "year", defecto, 2020, 2050, requerido)


def mes(request, requerido=True, defecto=None):
    return entero(request, "month", defecto, 1, 12, requerido)
