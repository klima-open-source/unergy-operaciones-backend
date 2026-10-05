"""Ingesta del costo regulatorio del mes desde el Drive de Estados de Resultados.

Reutiliza el plumbing de `apps/contabilidad/services/drive.py` (listar la carpeta de ER, parsear el
nombre, bajar el archivo) y el parser de `costo_regulatorio.py`. La
selección de período/versión es pura; `costo_regulatorio_del_mes` inyecta las funciones
de Drive para poder testear sin red.

Regla: para (año, mes) se toma el `Cruce facturas` de ESE período con la versión más
definitiva (txf > txN por número). Si no existe, fallback al último período disponible
que no sea posterior al pedido ("último disponible").
"""
from __future__ import annotations


def _rank_version(version) -> int:
    """txf es la final (más alta); txN vale N; desconocida cae al fondo."""
    v = str(version or "").strip().lower()
    if v == "txf":
        return 1000
    if v.startswith("tx"):
        try:
            return int(v[2:])
        except ValueError:
            return -1
    return -1


def seleccionar_cruce(cruces: list[dict], anio: int, mes: int) -> dict | None:
    """cruces = [{'id','anio','mes','version'}, ...] -> el cruce elegido con flag
    'fallback', o None si no hay ninguno.

    Elige el período == (anio, mes); si no hay, el mayor período <= (anio, mes). Dentro
    del período, la versión de mayor rank.
    """
    con_periodo = [c for c in cruces if c.get("anio") and c.get("mes")]
    if not con_periodo:
        return None
    objetivo = (anio, mes)
    exactos = [c for c in con_periodo if (c["anio"], c["mes"]) == objetivo]
    if exactos:
        elegido = max(exactos, key=lambda c: _rank_version(c["version"]))
        return {**elegido, "fallback": False}
    previos = [c for c in con_periodo if (c["anio"], c["mes"]) <= objetivo]
    if not previos:
        return None
    ultimo_periodo = max((c["anio"], c["mes"]) for c in previos)
    candidatos = [c for c in previos if (c["anio"], c["mes"]) == ultimo_periodo]
    elegido = max(candidatos, key=lambda c: _rank_version(c["version"]))
    return {**elegido, "fallback": True}


from apps.garantias.services.costo_regulatorio import costo_regulatorio_de_bytes


def _cruces_de_carpeta(listar) -> list[dict]:
    """Lista la carpeta de ER y deja solo los cruces, con período/versión parseados."""
    from apps.contabilidad.services.drive import TIPO_CRUCE, parse_nombre_er
    cruces = []
    for f in listar():
        if f.get("mimeType") == "application/vnd.google-apps.folder":
            continue
        info = parse_nombre_er(f.get("name", ""))
        if info["tipo"] == TIPO_CRUCE:
            cruces.append({"id": f.get("id"), "anio": info["anio"],
                           "mes": info["mes"], "version": info["version"]})
    return cruces


def costo_regulatorio_del_mes(anio: int, mes: int, *, listar=None, descargar=None) -> dict:
    """Costo regulatorio de (anio, mes) desde el Drive de ER, con fallback al último
    período disponible. Devuelve {'valor', 'anio', 'mes', 'version', 'fallback', 'cruce'}.
    `valor` es None si no hay ningún cruce. `listar`/`descargar` son inyectables (tests);
    por defecto usan `apps.contabilidad.services.drive`.
    """
    if listar is None or descargar is None:
        from apps.contabilidad.services.drive import descargar_archivo, er_folder_id, listar_carpeta
        if listar is None:
            listar = lambda: listar_carpeta(er_folder_id())
        if descargar is None:
            descargar = descargar_archivo

    cruces = _cruces_de_carpeta(listar)
    elegido = seleccionar_cruce(cruces, anio, mes)
    if elegido is None:
        return {"valor": None, "anio": anio, "mes": mes,
                "version": None, "fallback": False, "cruce": None}
    contenido = descargar(elegido["id"])
    valor = costo_regulatorio_de_bytes(contenido)
    return {
        "valor": valor,
        "anio": elegido["anio"],
        "mes": elegido["mes"],
        "version": elegido["version"],
        "fallback": elegido["fallback"],
        "cruce": elegido,
    }
