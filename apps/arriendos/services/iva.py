"""El IVA de un arrendador: la tasa de su ficha de cliente, o el 19% de siempre.

Hasta el 2026-09-18 el arrendador tenía un `responsable_iva` suelto --un sí/no--
y `calcular_iva` aplicaba **19% fijo**. Mientras tanto, los clientes ya tienen
tasas configurables por servicio y hasta por proyecto
(`cliente_tasa_servicio`), que es lo que usan las facturas de Representación,
CGM y Administración. Dos formas de responder a la misma pregunta.

Decidido con Sara: el IVA del arrendador se DERIVA de su cliente. Pero el cambio
se hace sin mover ninguna factura de golpe:

- Si el arrendador **no tiene cliente vinculado** --hoy, ninguno--, se comporta
  exactamente como antes: 19% si es responsable, nada si no.
- Si lo tiene pero **su cliente no tiene tasa configurada** para arriendo,
  también se comporta como antes. Vincular no cambia números por sí solo.
- Solo cuando alguien configura explícitamente la tasa de ese cliente, esa manda.

Así la derivación se enciende sola, cliente por cliente, y nunca de forma
inadvertida. `manage.py revisar_iva_arrendadores` dice de antemano a quién le
cambiaría el número.

**Sin verificar contra la base** (2026-09-18, sin acceso): qué valores usa
`cliente_tasa_servicio.servicio`. Las facturas de servicio guardan
"Representación"/"CGM"/"Administración" --con mayúscula y tilde--, así que acá se
compara el texto normalizado en vez de exigir una grafía exacta.
"""

import unicodedata

from apps.clientes.models import Cliente, ClienteTasaServicio

#: El 19% que aplicaba `calcular_iva` a todo arrendador responsable de IVA.
IVA_POR_DEFECTO_PCT = 19.0


def _clave(texto: str | None) -> str:
    """El texto en minúsculas y sin tildes, para comparar grafías distintas."""
    limpio = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in limpio if not unicodedata.combining(c)).strip().lower()


def _tasa_configurada(cliente_id: int, proyecto_id: int | None) -> float | None:
    """El `iva_pct` que el cliente tenga puesto para arriendo, si tiene alguno.

    Misma precedencia que el resto del sistema (`impuestos.tasas_efectivas`): la
    excepción del proyecto gana sobre la global del servicio, y esa sobre la
    general del cliente.
    """
    filas = [
        f for f in ClienteTasaServicio.objects.filter(cliente_id=cliente_id)
        if _clave(f.servicio) == "arriendo"
    ]
    for fila in filas:
        if fila.proyecto_id == proyecto_id and fila.iva_pct is not None:
            return float(fila.iva_pct)
    for fila in filas:
        if fila.proyecto_id is None and fila.iva_pct is not None:
            return float(fila.iva_pct)

    cliente = Cliente.objects.filter(pk=cliente_id).first()
    general = getattr(cliente, "iva_pct", None) if cliente else None
    return float(general) if general is not None else None


def pct_de(arrendador, proyecto_id: int | None = None) -> float:
    """El porcentaje de IVA de este arrendador. `0.0` si no se le factura IVA.

    El `responsable_iva` del arrendador sigue mandando sobre si se cobra o no:
    una tasa configurada dice CUÁNTO, no SI. Quitarle esa decisión al usuario
    haría que vincular un cliente empezara a cobrar IVA a quien no lo paga.
    """
    if not getattr(arrendador, "responsable_iva", False):
        return 0.0

    cliente_id = getattr(arrendador, "cliente_id", None)
    if not cliente_id:
        return IVA_POR_DEFECTO_PCT

    configurada = _tasa_configurada(cliente_id, proyecto_id)
    return configurada if configurada is not None else IVA_POR_DEFECTO_PCT
