"""Diagnóstico de coordenadas fuera de Colombia (sin Django: se prueba suelto).

Lo usa `manage.py revisar_coordenadas`. El rango es el mismo que `RANGO_COLOMBIA`
del front (`app/utils/coordenadas.ts`).
"""
from decimal import Decimal

#: Colombia con un margen (incluye San Andrés). Igual que `RANGO_COLOMBIA` del front.
LATITUD = (Decimal("-4.3"), Decimal("13.5"))
LONGITUD = (Decimal("-82"), Decimal("-66"))


def _dentro(valor, rango):
    return rango[0] <= valor <= rango[1]


def diagnosticar(lat, lng):
    """(problema, sugerencia) para un par, o None si está bien o vacío del todo."""
    if lat is None and lng is None:
        return None
    if lat is None or lng is None:
        return ("Falta una de las dos", "")
    if _dentro(lat, LATITUD) and _dentro(lng, LONGITUD):
        return None
    if _dentro(lng, LATITUD) and _dentro(lat, LONGITUD):
        return ("Latitud y longitud invertidas", f"{lng}, {lat}")
    if _dentro(lat, LATITUD) and _dentro(-lng, LONGITUD):
        return ("Longitud sin el signo menos", f"{lat}, {-lng}")
    if _dentro(-lng, LATITUD) and _dentro(-lat, LONGITUD) and lat < 0:
        return ("Invertidas y con el signo en la que no va", f"{-lng}, {lat}")
    return ("Fuera de Colombia", "")
