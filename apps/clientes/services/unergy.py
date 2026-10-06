"""Unergy como parte de un contrato.

En un PPA, una de las dos partes es SIEMPRE Unergy: en uno de compra Unergy
compra, en uno de venta Unergy vende. Hasta el 2026-09-18 ese lado se dejaba
vacío --`firmar()` escribía solo el cliente de la oferta-- y por eso no se podía
exigir que un contrato nombrara a sus dos partes.

Decidido con Sara: **Unergy es un cliente más**, una fila de `clientes` como
cualquier otra contraparte. La tabla ya se describe como "la razón social con la
que Unergy contrata", así que no hace falta un concepto nuevo; hace falta saber
CUÁL de esas filas es Unergy, y eso lo dice su NIT.

El NIT va en la configuración (`UNERGY_NIT`) y no en el código, porque es un dato
de la empresa y no una regla: cambiarlo no debería ser un despliegue. Se compara
solo por dígitos, igual que `gestion.normalizar_nit`, o "901.234.567-8" y
"9012345678" serían dos empresas distintas.
"""

from django.conf import settings

from apps.clientes.models import Cliente
from apps.clientes.services.gestion import normalizar_nit


def cliente_unergy() -> Cliente | None:
    """La fila de `clientes` que es Unergy, o `None` si no se puede saber.

    Devuelve `None` --en vez de reventar-- cuando `UNERGY_NIT` no está
    configurado o cuando ninguna fila tiene ese NIT. Quien llama decide qué
    hacer: crear un PPA con una parte vacía es peor que no crearlo, pero
    **bloquear al CRM porque falta un ajuste sería peor todavía**. El aviso viaja
    al usuario.

    Si dos filas comparten el NIT no se elige ninguna: son un duplicado que hay
    que fusionar (`/clientes/{ganador}/merge/{perdedor}`), y elegir al azar ataría
    los contratos a la mitad equivocada.
    """
    clave = normalizar_nit(getattr(settings, "UNERGY_NIT", None))
    if not clave:
        return None

    iguales = [
        c for c in Cliente.objects.filter(
            deleted_at__isnull=True, nit_cedula__isnull=False,
        )
        if normalizar_nit(c.nit_cedula) == clave
    ]
    return iguales[0] if len(iguales) == 1 else None
