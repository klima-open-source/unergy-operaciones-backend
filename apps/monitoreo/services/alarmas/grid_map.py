"""
Mapeo de minigranjas a infraestructura de red electrica colombiana.

Jerarquia: OR (Operador de Red) -> Subestacion -> Circuito -> Proyecto
"""
from __future__ import annotations

import re

GRID_INFO: dict[str, dict[str, str]] = {
    "Valle de Gandalf":  {"or": "Afinia", "region": "Cesar", "subestacion": "La Paz",       "circuito": "LN 749"},
    "Cañahuate":         {"or": "Afinia", "region": "Cesar", "subestacion": "La Paz",       "circuito": "LN 749"},
    "San Diego Sur":     {"or": "Afinia", "region": "Cesar", "subestacion": "La Paz",       "circuito": "LN 749"},
    "La Paz Vallenata":  {"or": "Afinia", "region": "Cesar", "subestacion": "La Paz",       "circuito": "LINEA 598"},
    "La Paz Verso":      {"or": "Afinia", "region": "Cesar", "subestacion": "La Paz",       "circuito": "LINEA 598"},
    "La Paz Leyenda":    {"or": "Afinia", "region": "Cesar", "subestacion": "La Paz",       "circuito": "LINEA 598"},
    "La Paz Esmeralda":  {"or": "Afinia", "region": "Cesar", "subestacion": "La Paz",       "circuito": "LINEA 598"},
    "El Son":            {"or": "Afinia", "region": "Cesar", "subestacion": "Salguero",     "circuito": "SALGUERO 2"},
    "El Merengue":       {"or": "Afinia", "region": "Cesar", "subestacion": "Salguero",     "circuito": "SALGUERO 2"},
    "La Puya":           {"or": "Afinia", "region": "Cesar", "subestacion": "Salguero",     "circuito": "SALGUERO 2"},
    "El Olimpo":         {"or": "Afinia", "region": "Cesar", "subestacion": "Salguero",     "circuito": "SALGUERO 2"},
    "La Mesa":           {"or": "Afinia", "region": "Cesar", "subestacion": "Salguero",     "circuito": "SALGUERO 2"},
    "Perijá":            {"or": "Afinia", "region": "Cesar", "subestacion": "Chiriguaná",   "circuito": "LN 571"},
    "Ibiríco":           {"or": "Afinia", "region": "Cesar", "subestacion": "Chiriguaná",   "circuito": "LN 571"},
    "Chiriguaná 1":      {"or": "Afinia", "region": "Cesar", "subestacion": "Chiriguaná",   "circuito": "LN 571"},
    "La Cumbia":         {"or": "Afinia", "region": "Cesar", "subestacion": "Chiriguaná",   "circuito": "LN 571"},
    "Piloneras":         {"or": "Afinia", "region": "Cesar", "subestacion": "Chiriguaná",   "circuito": "LN 571"},
    "La Cacica":         {"or": "Air-e",  "region": "Cesar", "subestacion": "?",            "circuito": "POLO NUEVO"},
    "El Copey":          {"or": "Afinia", "region": "Cesar", "subestacion": "El Copey",     "circuito": "LINEA 590"},
    "Agustín 1":         {"or": "Afinia", "region": "Cesar", "subestacion": "Codazzi",      "circuito": "LINEA 559"},
    "La Catedral":       {"or": "Afinia", "region": "Cesar", "subestacion": "Codazzi",      "circuito": "LINEA 559"},
    "Marimonda":         {"or": "Afinia", "region": "Cesar", "subestacion": "Codazzi",      "circuito": "LINEA 559"},
    "Valencia Or_1":     {"or": "Afinia", "region": "Cordoba", "subestacion": "Valencia",   "circuito": "VALENCIA DE JESUS URBANO"},
    "Valencia Or_2":     {"or": "Afinia", "region": "Cordoba", "subestacion": "Valencia",   "circuito": "VALENCIA DE JESUS URBANO"},
    "El Molino":         {"or": "Air-e",  "region": "La Guajira", "subestacion": "?",       "circuito": "MOLINO"},
    "Villanueva":        {"or": "Air-e",  "region": "La Guajira", "subestacion": "?",       "circuito": "URUMITA"},
    "Uruaco":            {"or": "Air-e",  "region": "Atlantico",  "subestacion": "?",       "circuito": "LURUACO"},
    "Los Bongos":        {"or": "Air-e",  "region": "Atlantico",  "subestacion": "?",       "circuito": "GALAPA"},
    "La Reserva":        {"or": "ESSA",   "region": "Santander",  "subestacion": "?",       "circuito": "?"},
    "Baraya":            {"or": "Afinia", "region": "Sucre",   "subestacion": "Galeras",    "circuito": "LN 5113"},
    "El Roble":          {"or": "Afinia", "region": "Sucre",   "subestacion": "?",          "circuito": "?"},
    "San Pedro":         {"or": "Afinia", "region": "Sucre",   "subestacion": "?",          "circuito": "LN 5146"},
    "San Pelayo":        {"or": "Afinia", "region": "Cordoba", "subestacion": "?",          "circuito": "?"},
    "Sol y Cielo 9 Cienaga": {"or": "Afinia", "region": "Cordoba", "subestacion": "Ciénaga de Oro", "circuito": "LINEA 517"},
    "Ladrillera Del Meta": {"or": "?",   "region": "Meta",    "subestacion": "?",          "circuito": "?"},
    "SIRIUS":            {"or": "?",      "region": "?",       "subestacion": "?",          "circuito": "?"},
    "Yuan Solar":        {"or": "?",      "region": "?",       "subestacion": "?",          "circuito": "?"},
}

_UNKNOWN = {"or": "?", "region": "?", "subestacion": "?", "circuito": "?"}
_CODE_PREFIX = re.compile(r"^\d{4}\s*-\s*")


def get_grid_info(proj_name: str) -> dict[str, str]:
    clean = _CODE_PREFIX.sub("", proj_name).strip()
    return GRID_INFO.get(clean, _UNKNOWN)


def group_by_grid(proj_names: list[str], level: str = "subestacion") -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for name in proj_names:
        info = get_grid_info(name)
        key = info.get(level, "?")
        groups.setdefault(key, []).append(name)
    return groups
