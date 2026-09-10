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


def enteros(request, nombre):
    """`?ppa_id=12&ppa_id=45` — un parámetro repetible, como lista de enteros.

    FastAPI lo declaraba `list[int] | None = Query(None)` y devolvía 422 con
    solo verlo en la firma. Media docena de vistas lo parsean a mano con
    `[int(v) for v in getlist(...)]`, que ante un valor no numérico levanta un
    `ValueError` y sale un **500**: peor que un 422, porque parece una caída.

    Un valor vacío (`?ppa_id=`) se ignora, igual que en `entero`.
    """
    crudos = [v for v in request.query_params.getlist(nombre) if v not in (None, "")]
    valores = []
    for crudo in crudos:
        try:
            valores.append(int(crudo))
        except (TypeError, ValueError):
            raise NoProcesable(
                f"'{nombre}' debe ser un número entero ('{crudo}' no lo es)"
            )
    return valores


_VERDADEROS = frozenset({"1", "true", "yes", "on", "t", "y"})
_FALSOS = frozenset({"0", "false", "no", "off", "f", "n"})


def bandera(request, nombre, defecto=False):
    """`?incluir_todos=true`. El mismo juego de valores que aceptaba FastAPI.

    **Un valor que no sea booleano da 422, no `False`.** Antes cualquier cosa
    fuera de la lista de verdaderos caía a `False` en silencio, así que un
    `?solo_activas=activas` o un typo devolvía el listado completo con 200 y sin
    una sola señal de que el filtro nunca se aplicó. FastAPI lo rechazaba por la
    firma del endpoint.

    **La cadena vacía cuenta como ausente** y devuelve el default, igual que
    `entero` y `fecha`. Antes `?dry_run=` daba `False`: el backfill de SLA, cuyo
    default es `True` justamente para no escribir sin que se lo pidan, corría en
    firme. Ahora respeta su default.
    """
    crudo = request.query_params.get(nombre)
    if crudo in (None, ""):
        return defecto

    valor = crudo.strip().lower()
    if valor in _VERDADEROS:
        return True
    if valor in _FALSOS:
        return False
    raise NoProcesable(
        f"'{nombre}' debe ser un booleano ('{crudo}' no lo es). "
        f"Valores válidos: {', '.join(sorted(_VERDADEROS | _FALSOS))}."
    )


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
