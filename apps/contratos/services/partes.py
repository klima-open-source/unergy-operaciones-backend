"""Resolver el cliente de las partes de un contrato de servicio.

La auditoría de Clientes del 2026-08-27 encontró 0 de 162 contratos con el
vínculo puesto, y sin él las «condiciones económicas» del panel 360 y otras
vistas de clientes salían vacías. Eran DOS causas a la vez: el wizard no obligaba
a elegir del autocompletado, y `ContratoEscrituraSerializer` descartaba en
silencio la clave `contratante_id` que el frontend sí mandaba (el FK se llama
`contratante`, así que el campo generado también). Las dos están corregidas.

Esto queda como RED, no como vía principal: un contrato que llega con el vínculo
puesto no pasa por `resolver_cliente_id`. Sigue haciendo falta para lo que entra
por otros caminos y para los contratos viejos, pero adivinar por nombre es
siempre el último recurso.
"""

from apps.clientes import models as cl_models
from apps.comun.nombre_matching import core_tokens, mejor_candidato


def _solo_alfanumerico(texto: str) -> str:
    return "".join(c for c in (texto or "") if c.isalnum())


def resolver_cliente_id(nombre: str | None, nit: str | None) -> int | None:
    """Primero por NIT exacto normalizado; si no, por nombre parecido.

    El nombre parecido exige ADEMÁS solapamiento real de tokens, no solo
    similitud de caracteres: el backfill manual encontró casos reales como
    "BALI ENERGY S.A.S." emparejando con "INENERGY S.A.S." — cero tokens en
    común, solo letras parecidas.

    Un NIT que casa con DOS clientes no resuelve nada: se ignora y se pasa al
    nombre, porque elegir uno al azar ataría el contrato al cliente equivocado.
    """
    if nit:
        clave = _solo_alfanumerico(nit)
        if clave:
            iguales = [
                c for c in cl_models.Cliente.objects
                .filter(deleted_at__isnull=True, nit_cedula__isnull=False)
                if _solo_alfanumerico(c.nit_cedula) == clave
            ]
            if len(iguales) == 1:
                return iguales[0].id

    if nombre:
        clientes = list(
            cl_models.Cliente.objects.filter(deleted_at__isnull=True)
        )
        candidato, _score = mejor_candidato(
            nombre, [(c, [c.razon_social_nombre]) for c in clientes]
        )
        if candidato and (
            core_tokens(nombre) & core_tokens(candidato.razon_social_nombre)
        ):
            return candidato.id
    return None


# El inversionista entra a la lista aunque no tenga columna de NIT: es el que
# usa el reparto de costos para saber qué tarifa le toca a cada quien
# (`apps/contabilidad/services/costos.py`), y mientras su vínculo esté vacío ese
# cálculo no tiene más remedio que emparejar comparando nombres.
ROLES = ("contratante", "prestador", "inversionista")


def sincronizar(contrato) -> None:
    """Resuelve los vínculos que falten y copia nombre y NIT del cliente."""
    campos = []
    for rol in ROLES:
        tiene_nit = hasattr(contrato, f"{rol}_nit")
        if not getattr(contrato, f"{rol}_id"):
            resuelto = resolver_cliente_id(
                getattr(contrato, f"{rol}_nombre"),
                getattr(contrato, f"{rol}_nit") if tiene_nit else None,
            )
            if resuelto:
                setattr(contrato, f"{rol}_id", resuelto)
                campos.append(rol)

        cliente_id = getattr(contrato, f"{rol}_id")
        if not cliente_id:
            continue
        cliente = cl_models.Cliente.objects.filter(pk=cliente_id).first()
        if cliente is None:
            continue
        setattr(contrato, f"{rol}_nombre", cliente.razon_social_nombre)
        campos.append(f"{rol}_nombre")
        if tiene_nit:
            setattr(contrato, f"{rol}_nit", cliente.nit_cedula)
            campos.append(f"{rol}_nit")

    if campos:
        contrato.save(update_fields=list(dict.fromkeys(campos)))
