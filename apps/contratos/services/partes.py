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
from apps.comun.nombre_matching import core_tokens, mejor_candidato, normalizar


def _solo_alfanumerico(texto: str) -> str:
    return "".join(c for c in (texto or "") if c.isalnum())


#: Palabras que comparten demasiados clientes para decir quién es quién. Compartir
#: SOLO una de estas no es parecido: el 2026-10-07, con datos de producción,
#: "Bia Energy" casaba con "BALI ENERGY", "NITRO ENERGY COLOMBIA" con "CSCI
#: COLOMBIA SOLAR CORP" y "PROMOTORA DE ENERGIA ELECTRICA DE CARTAGENA" con "SOL Y
#: CIELO ENERGIA", cada uno por una sola palabra de esta lista.
PALABRAS_GENERICAS = frozenset({
    "energy", "energia", "energias", "energetica", "energeticas", "electrica",
    "electricas", "power", "solar", "solares", "renovable", "renovables",
    "sostenible", "sostenibles", "verde", "green", "colombia", "colombiana",
    "inversiones", "inversion", "investment", "grupo", "group", "holding",
    "capital", "activos", "servicios", "soluciones", "digital", "comercializadora",
    "promotora", "desarrollos", "proyectos", "internacional", "andina",
})

#: Cómo se emparejó. Solo los dos primeros son seguros para escribir sin que una
#: persona lo mire (`vincular_partes_contratos`).
POR_NIT = "nit"
POR_NOMBRE = "nombre"
PARECIDO = "parecido"


def _palabras(nombre: str | None) -> frozenset:
    return frozenset(normalizar(nombre or "").split())


def emparejar_cliente(nombre: str | None, nit: str | None) -> tuple[int | None, str | None]:
    """`(cliente_id, cómo)`: por NIT exacto, por el mismo nombre, o solo parecido.

    1. **NIT** exacto, normalizado. Un NIT que casa con DOS clientes no resuelve
       nada: se pasa al nombre, porque elegir uno al azar ataría el contrato al
       cliente equivocado.
    2. **El mismo nombre**: las mismas palabras, sin tildes, puntuación ni sufijo
       societario, en cualquier orden ("Beatriz Rodriguez Velez" = "RODRIGUEZ
       VELEZ BEATRIZ"). Si dos clientes se llaman igual, no resuelve.
    3. **Parecido**: el mejor candidato por similitud, que además comparta al
       menos una palabra PROPIA (`PALABRAS_GENERICAS` no cuentan) — sin eso,
       "BALI ENERGY S.A.S." casaba con "INENERGY S.A.S." por letras parecidas.
       Es una sugerencia: puede ser otra empresa de la misma familia
       ("Mauricio Estrada Arbelaez" → "INVERSIONES ESTRADA ARBELAEZ").
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
                return iguales[0].id, POR_NIT

    if not (nombre or "").strip():
        return None, None
    clientes = list(cl_models.Cliente.objects.filter(deleted_at__isnull=True))

    palabras = _palabras(nombre)
    iguales = [c for c in clientes if palabras and _palabras(c.razon_social_nombre) == palabras]
    if len(iguales) == 1:
        return iguales[0].id, POR_NOMBRE

    candidato, _score = mejor_candidato(
        nombre, [(c, [c.razon_social_nombre]) for c in clientes]
    )
    if candidato and (
        (core_tokens(nombre) - PALABRAS_GENERICAS)
        & core_tokens(candidato.razon_social_nombre)
    ):
        return candidato.id, PARECIDO
    return None, None


def resolver_cliente_id(nombre: str | None, nit: str | None) -> int | None:
    """El cliente de una parte, incluido el solo parecido (ver `emparejar_cliente`).

    Lo usa `sincronizar` al guardar un contrato que llega SIN vínculo — hoy la
    pantalla y la API ya lo exigen al crear, así que es la red para lo que entra
    por otros caminos—. El backfill masivo NO acepta el parecido: lo deja para
    revisar a mano.
    """
    return emparejar_cliente(nombre, nit)[0]


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
