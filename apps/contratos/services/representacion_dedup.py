"""Deteccion y fusion de contratos de representacion duplicados.

Produccion acumulo el mismo contrato escrito por tres fuentes distintas, y
ninguna reconocia a las otras:

  - `scripts/seed_contratos_cgm.py` — dejo inversionista y tarifas, pero hace
    `c.pop("proyecto_nombre")` y nunca guarda `nombre_proyecto_ref`.
  - `_seed_contratos_cgm` en `app/main.py` — dejo inversionista, tarifas y
    `nombre_proyecto_ref`.
  - el wizard, a mano — dejo numero de contrato, fechas y el PDF, pero **no**
    `inversionista_nombre`.

Resultado: MGS Naos 2 tiene tres filas y es un solo contrato.

El arreglo del seed (ver `_CgmIndice`) evita que se creen mas, pero no limpia lo
que ya esta. Este modulo hace eso, con una regla que no puede perder datos: se
fusiona SOLO cuando los registros no se contradicen. Si dos filas dicen cosas
distintas en el mismo campo, el grupo se marca para revision humana y no se
toca.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable


def norm(s: Any) -> str:
    """Normaliza para comparar: solo letras y digitos, en mayusculas.

    Mas agresiva que `_cgm_norm` de `main.py`, que conserva los espacios: alla
    hacen falta porque el matcher busca el numero de la planta como substring y
    pegar digitos crearia coincidencias que no existen. Aca solo se compara si
    dos valores del mismo campo dicen lo mismo, asi que conviene que "S.A.S" y
    "SAS" colapsen -- si no, una diferencia de puntuacion se leeria como una
    contradiccion y bloquearia una fusion legitima.
    """
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", "", s.upper())


# Campos que definen el contrato y que se completan al fusionar. Se excluyen id,
# timestamps y `servicio_aplica` (siempre 'representacion' en este universo).
CAMPOS = (
    "proyecto_id", "numero_contrato",
    "contratante_id", "contratante_nombre", "contratante_nit",
    "prestador_id", "prestador_nombre", "prestador_nit",
    "inversionista_nombre", "portafolio", "codigo_sun_factory", "nombre_proyecto_ref",
    "fecha_inicio", "fecha_fin", "fecha_firma_contrato", "fecha_indexacion",
    "fecha_inicio_om", "renovacion_automatica", "periodicidad_pago",
    "indice_indexacion", "tarifa_base", "tarifa_mensual",
    "tarifa_admin", "tarifa_cgm", "tarifa_representacion",
    "indexacion_cgm", "indexacion_representacion",
    "enlace_drive", "estado_pago", "estado",
    "cgm_codigo_sic",
    "responsable_iva",
)

# Campos cuyo valor por defecto no es una afirmacion: el seed pone estado
# 'vigente' y los booleanos en False sin que nadie lo haya decidido, asi que una
# diferencia ahi no es un conflicto real que deba bloquear la fusion.
CAMPOS_BLANDOS = frozenset({
    "estado", "responsable_iva",
})


def _vacio(v: Any) -> bool:
    if v is None or v == "":
        return True
    if isinstance(v, (list, tuple, dict)) and len(v) == 0:
        return True
    return False


def _iguales(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return norm(a) == norm(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


_TOPE_TEXTO = 160


def _describir(valores: list[Any]) -> list[str]:
    """Vuelve los valores en conflicto texto legible, sin repetir.

    No se puede deduplicar con `set` ni con `dict.fromkeys`: dos de los campos en
    juego —`indexacion_cgm` y `indexacion_representacion`— son listas de dicts, y
    una lista no es hashable. Eso reventaba con TypeError en cuanto dos registros
    traian indexaciones distintas, que es justo el caso de MGS Naos 3.

    Tambien se recorta: una indexacion de diez aniversarios convertida a texto
    haria ilegible el informe.
    """
    salida: list[str] = []
    for v in valores:
        t = str(v)
        if len(t) > _TOPE_TEXTO:
            t = t[:_TOPE_TEXTO] + "…"
        if t not in salida:
            salida.append(t)
    return salida


def _valor(reg: Any, campo: str) -> Any:
    return reg.get(campo) if isinstance(reg, dict) else getattr(reg, campo, None)


def _id(reg: Any) -> Any:
    return _valor(reg, "id")


def _llenos(reg: Any) -> int:
    return sum(1 for c in CAMPOS if not _vacio(_valor(reg, c)))


def agrupar(contratos: Iterable[Any]) -> list[list[Any]]:
    """Agrupa los contratos que son el mismo, por (inversionista + planta).

    Un registro sin inversionista —los del wizard— se une al grupo de su planta
    solo si esa planta tiene UN unico inversionista. Con varios (Baraya tiene
    tres) no hay forma de saber a cual pertenece, asi que se deja aparte.
    """
    contratos = list(contratos)
    grupos: dict[tuple, list[Any]] = {}
    sin_inv: dict[Any, list[Any]] = {}

    for reg in contratos:
        inv = norm(_valor(reg, "inversionista_nombre"))
        pid = _valor(reg, "proyecto_id")
        if not inv:
            # Sin planta tampoco hay a que pegarlo.
            if pid is not None:
                sin_inv.setdefault(pid, []).append(reg)
            continue
        # La identidad de la planta es proyecto_id; si falta, el nombre de
        # referencia o el codigo Sun Factory.
        planta = ("pid", pid) if pid is not None else (
            "ref", norm(_valor(reg, "nombre_proyecto_ref"))
            or norm(_valor(reg, "codigo_sun_factory")))
        grupos.setdefault((inv, planta), []).append(reg)

    # Repartir los del wizard
    for pid, huerfanos in sin_inv.items():
        invs = {k[0] for k in grupos if k[1] == ("pid", pid)}
        if len(invs) == 1:
            grupos[(invs.pop(), ("pid", pid))].extend(huerfanos)

    return [g for g in grupos.values() if len(g) > 1]


def analizar(grupo: list[Any]) -> dict:
    """Decide si un grupo se puede fusionar y con que valores.

    Devuelve {fusionable, conservar, eliminar, valores, conflictos}.
    `valores` son solo los campos que hay que escribir en el registro que se
    conserva; `conflictos` lista los campos donde los registros se contradicen.
    """
    # Se conserva el que mas datos tenga; a igualdad, el de id mas bajo (el mas
    # antiguo), para que la eleccion sea estable entre corridas.
    orden = sorted(grupo, key=lambda r: (-_llenos(r), _id(r) if _id(r) is not None else 0))
    conservar, resto = orden[0], orden[1:]

    valores: dict[str, Any] = {}
    conflictos: list[dict] = []

    for campo in CAMPOS:
        presentes = [(r, _valor(r, campo)) for r in grupo if not _vacio(_valor(r, campo))]
        if not presentes:
            continue
        base = presentes[0][1]
        discrepa = [v for _, v in presentes[1:] if not _iguales(base, v)]
        if discrepa and campo not in CAMPOS_BLANDOS:
            conflictos.append({"campo": campo, "valores": _describir([base, *discrepa])})
            continue
        # Sin contradiccion: si al que se conserva le falta, se completa.
        if _vacio(_valor(conservar, campo)):
            valores[campo] = base

    return {
        "fusionable": not conflictos,
        "conservar": _id(conservar),
        "eliminar": [_id(r) for r in resto],
        "valores": valores,
        "conflictos": conflictos,
    }


def revisar(contratos: Iterable[Any]) -> dict:
    """Informe completo: que se puede fusionar y que necesita ojo humano."""
    fusionables, con_conflicto = [], []
    for grupo in agrupar(contratos):
        r = analizar(grupo)
        etiqueta = {
            "inversionista": _valor(grupo[0], "inversionista_nombre")
                             or _valor(grupo[-1], "inversionista_nombre"),
            "proyecto_id": _valor(grupo[0], "proyecto_id"),
            "planta": next((_valor(r_, "nombre_proyecto_ref") for r_ in grupo
                            if _valor(r_, "nombre_proyecto_ref")), None),
            "ids": [_id(r_) for r_ in grupo],
        }
        (fusionables if r["fusionable"] else con_conflicto).append({**etiqueta, **r})
    return {
        "grupos_fusionables": fusionables,
        "grupos_con_conflicto": con_conflicto,
        "contratos_a_eliminar": sum(len(g["eliminar"]) for g in fusionables),
    }
