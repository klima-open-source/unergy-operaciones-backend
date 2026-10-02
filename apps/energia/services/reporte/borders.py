"""Resolución de `frt_code` → border de Quoia.

Puerto de `resolver_borders` de `app/services/reporte_cgm.py`. Vive acá porque
lo consumen tanto el reporte de energía (`/enviar`) como el
reporte CGM, y las dos versiones tienen que leer el MISMO catálogo.

Usa `curvas.obtener_borders_crudos`, cacheado 30 min: antes cada consumidor
llamaba a `gaia.get_all_borders()` por su cuenta con su propia caché, así que una
petición que dispara los dos pagaba el fetch completo del catálogo dos veces
(auditoría CGM 2026-08-26).
"""

from __future__ import annotations

from apps.energia.services.reporte import curvas


def resolver_borders(gaia, frt_codes: set[str]) -> dict[str, dict]:
    """`{frt_code en minúsculas: {id, category, name}}`, solo para los pedidos."""
    buscados = {c.lower() for c in frt_codes}
    resultado: dict[str, dict] = {}
    for proyecto in curvas.obtener_borders_crudos(gaia):
        nombre = (proyecto.get("name") or "").strip()
        for clave in ("frt_generation", "frt_consumption"):
            frt = proyecto.get(clave)
            if not frt:
                continue
            frt_code = (frt.get("frt_code") or "").strip().lower()
            if frt_code in buscados:
                resultado[frt_code] = {
                    "id": frt.get("id"),
                    "category": frt.get("category"),
                    "name": nombre,
                }
    return resultado
