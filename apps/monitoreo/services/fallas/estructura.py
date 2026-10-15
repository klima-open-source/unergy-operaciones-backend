"""Estructura canónica del reporte de fallas (jerárquico por activo).

Fuente ÚNICA de la jerarquía Sistema → opciones. La consumen:
- El endpoint ``GET /fallas/estructura`` (form web + móvil).
- El sembrado idempotente de ``fallas_cat_categorias`` / ``fallas_cat_tipos``
  (para que las vistas/analytics legacy sigan mostrando ``falla.tipo.etiqueta``).
- La validación del camino estructurado en ``POST /fallas``.

Las fallas viejas (planas) NO dependen de esto; la estructura es aditiva.
"""
from __future__ import annotations

from enum import StrEnum

# Categorías de sistema. `tipo` describe qué inputs adicionales pide cada categoría:
#   - "opcion":   una sola opción de `opciones` (red, eventos_adversos)
#   - "equipo":   una sola opción de `opciones` + flags de frontera
#   - "inversores": multiselect de inversores del proyecto + multiselect de `tipos_falla`
ESTRUCTURA_FALLAS: list[dict] = [
    {
        "codigo": "red",
        "etiqueta": "Red",
        "icono": "pi pi-bolt",
        "color_hex": "#F59E0B",
        "tipo": "opcion",
        "descripcion": "Eventos del suministro eléctrico externo al proyecto.",
        "opciones": [
            {"codigo": "baja_tension", "etiqueta": "Baja tensión"},
            {"codigo": "alta_tension", "etiqueta": "Alta tensión"},
            {"codigo": "variacion_frecuencia", "etiqueta": "Variación de frecuencia"},
            {
                "codigo": "mantenimiento_red",
                "etiqueta": "Mantenimiento de red",
                "requiere_detalle": True,
                "detalle_label": "Motivo (árbol sobre la línea, fusible disparado, mant. programado, cambio de poste…)",
            },
            {"codigo": "acometida_mt", "etiqueta": "Acometida en media tensión"},
            {"codigo": "transformador", "etiqueta": "Transformador"},
            {
                "codigo": "desconexion_sin_identificar",
                "etiqueta": "Desconexión sin identificar",
                "pendiente_reclasificar": True,
                "descripcion": "Estado temporal: queda pendiente hasta reclasificar con la causa definitiva.",
            },
        ],
    },
    {
        "codigo": "frontera",
        "etiqueta": "Frontera",
        "icono": "pi pi-gauge",
        "color_hex": "#0EA5E9",
        "tipo": "equipo",
        "descripcion": "Equipos asociados a la medición comercial.",
        "opciones": [
            {"codigo": "medidor_principal", "etiqueta": "Medidor principal"},
            {"codigo": "medidor_respaldo", "etiqueta": "Medidor de respaldo"},
            {"codigo": "ct", "etiqueta": "CT (Transformadores de corriente)"},
            {"codigo": "pt", "etiqueta": "PT (Transformadores de potencial)"},
            {"codigo": "caja_pruebas", "etiqueta": "Caja de pruebas / Hornera"},
            {"codigo": "modem_comunicaciones", "etiqueta": "Módem de comunicaciones"},
        ],
        "flags": [
            {"codigo": "afecta_medicion", "etiqueta": "Afecta la medición de la frontera"},
            {
                "codigo": "perdida_comunicacion",
                "etiqueta": "Pérdida de comunicación (datos) de la frontera",
                "alarma": "comunicacion_frontera",
            },
        ],
    },
    {
        "codigo": "inversores",
        "etiqueta": "Inversores",
        "icono": "pi pi-server",
        "color_hex": "#915BD8",
        "tipo": "inversores",
        "descripcion": "Inversores del proyecto (lista parametrizable por proyecto).",
        "requiere_proyecto_unico": True,
        "tipos_falla": [
            {"codigo": "baja_tension_ac", "etiqueta": "Baja tensión AC"},
            {"codigo": "baja_tension_dc", "etiqueta": "Baja tensión DC"},
            {"codigo": "baja_resistencia_aislamiento", "etiqueta": "Baja resistencia de aislamiento"},
            {"codigo": "problemas_ventilacion", "etiqueta": "Problemas de ventilación"},
            {"codigo": "falla_dispositivo", "etiqueta": "Falla del dispositivo"},
            {"codigo": "problema_cadena_fotovoltaica", "etiqueta": "Problema en cadena fotovoltaico"},
            {"codigo": "sobre_temperatura", "etiqueta": "Sobre temperatura"},
            {"codigo": "arco_ac", "etiqueta": "Arco en AC"},
            {"codigo": "arco_dc", "etiqueta": "Arco en DC"},
            {
                "codigo": "perdida_comunicacion",
                "etiqueta": "Pérdida de comunicación (internet)",
                "alarma": "comunicacion_inversores",
            },
        ],
    },
    {
        "codigo": "equipos",
        "etiqueta": "Equipos de planta",
        "icono": "pi pi-cog",
        "color_hex": "#8B5CF6",
        "tipo": "opcion",
        "descripcion": "Equipos de la planta fuera de la medición comercial y de los inversores.",
        "opciones": [
            {"codigo": "smartlogger", "etiqueta": "Smartlogger"},
            {
                "codigo": "tracker",
                "etiqueta": "Tracker",
                "requiere_detalle": True,
                "detalle_label": "¿Cuál tracker? (fila, número…)",
            },
            {
                "codigo": "string",
                "etiqueta": "String",
                "requiere_detalle": True,
                "detalle_label": "¿Cuál string? (inversor y número…)",
            },
            {"codigo": "estacion_meteorologica", "etiqueta": "Estación meteorológica"},
        ],
    },
    {
        "codigo": "generando_sin_datos",
        "etiqueta": "Sistema generando pero sin datos",
        "icono": "pi pi-question-circle",
        "color_hex": "#64748B",
        "tipo": "opcion",
        "descripcion": "No llegan datos de generación y desde el monitoreo no se puede afirmar si el proyecto generó o no.",
        # Las opciones no son "eventos": son el resultado de verificar en sitio.
        "opciones_label": "Verificación en sitio",
        "opciones": [
            {
                "codigo": "incertidumbre",
                "etiqueta": "Incertidumbre (no sabemos si generó o no)",
                "pendiente_reclasificar": True,
                "descripcion": "Sin verificación en sitio todavía: queda pendiente hasta confirmar si el proyecto estaba encendido o apagado.",
            },
            {
                "codigo": "verificado_encendido",
                "etiqueta": "Verificado en sitio: proyecto encendido",
                "descripcion": "Se verificó en sitio que el proyecto está generando: la falla es solo de datos/monitoreo.",
            },
            {
                "codigo": "verificado_apagado",
                "etiqueta": "Verificado en sitio: proyecto apagado",
                "descripcion": "Se verificó en sitio que el proyecto no está generando.",
            },
        ],
    },
    {
        "codigo": "eventos_adversos",
        "etiqueta": "Eventos naturales",
        "icono": "pi pi-exclamation-triangle",
        "color_hex": "#EF4444",
        "tipo": "opcion",
        "descripcion": "Eventos naturales o climáticos que afectan la operación del proyecto.",
        "opciones": [
            {"codigo": "incendio", "etiqueta": "Incendio"},
            {"codigo": "inundacion", "etiqueta": "Inundación"},
            {"codigo": "huracan", "etiqueta": "Huracán"},
            {"codigo": "clima_nublado_lluvioso", "etiqueta": "Clima nublado/lluvioso"},
            {"codigo": "otro", "etiqueta": "Otro", "requiere_detalle": True, "detalle_label": "Describe el evento"},
        ],
    },
]

CATEGORIA_CODIGOS = {c["codigo"] for c in ESTRUCTURA_FALLAS}

# Tipos de falla de inversores retirados de las opciones (ya no se ofrecen al
# reportar), pero conservados para resolver la etiqueta de fallas históricas y
# no degradar informes/vistas ya guardados. NO reintroducir en tipos_falla.
INVERSOR_TIPOS_LEGACY = {
    "no_generacion": "No generación",
    "generacion_anomala": "Generación anómala",
    "limitacion_potencia": "Limitación de potencia",
    "strings_mal_conectados": "Strings mal conectados",
}

INVERSOR_TIPO_FALLA_CODIGOS = {
    t["codigo"]
    for c in ESTRUCTURA_FALLAS
    if c["codigo"] == "inversores"
    for t in c["tipos_falla"]
}


def get_categoria(codigo: str) -> dict | None:
    """Devuelve el dict de la categoría por código (o None)."""
    for c in ESTRUCTURA_FALLAS:
        if c["codigo"] == codigo:
            return c
    return None


def get_opcion(categoria_codigo: str, subtipo_codigo: str) -> dict | None:
    """Devuelve el dict de la opción/equipo dentro de una categoría (o None)."""
    cat = get_categoria(categoria_codigo)
    if not cat:
        return None
    for op in cat.get("opciones", []):
        if op["codigo"] == subtipo_codigo:
            return op
    return None


def tipo_codigo(categoria_codigo: str, subtipo_codigo: str) -> str:
    """Código global y único para sembrar/buscar el FallaCatTipo correspondiente."""
    return f"{categoria_codigo}.{subtipo_codigo}"


def codigos_estructurados() -> set[str]:
    """Todos los `FallaCatTipo.codigo` que esta estructura posee.

    Se deriva de ESTRUCTURA_FALLAS, no de una lista aparte, para que agregar una
    categoria no obligue a tocar nada mas -- es el mismo criterio que ya sigue
    `_aplicar_clasificacion`.

    Existe porque hay DOS taxonomias vivas en `fallas_cat_tipos`: la numerica
    legacy ("2.1") y esta ("red.baja_tension"). Cualquier cosa que necesite
    distinguirlas tiene que preguntar aca, no adivinar por la forma del codigo.
    """
    codigos: set[str] = set()
    for cat in ESTRUCTURA_FALLAS:
        for clave in ("opciones", "tipos_falla"):
            for item in cat.get(clave, []):
                codigos.add(tipo_codigo(cat["codigo"], item["codigo"]))
    return codigos


def etiqueta_subtipo(categoria_codigo: str, subtipo_codigo: str) -> str | None:
    """Etiqueta legible de una opción/equipo/tipo de falla de inversor."""
    cat = get_categoria(categoria_codigo)
    if not cat:
        return None
    pool = list(cat.get("opciones", [])) + list(cat.get("tipos_falla", []))
    for op in pool:
        if op["codigo"] == subtipo_codigo:
            return op["etiqueta"]
    # Respaldo: tipos de inversores retirados (fallas históricas conservan su etiqueta)
    if categoria_codigo == "inversores":
        return INVERSOR_TIPOS_LEGACY.get(subtipo_codigo)
    return None


def validar_clasificacion(
    categoria_codigo: str | None,
    subtipo_codigo: str | None,
    inversores_tipos: list[str] | None = None,
) -> tuple[bool, str | None]:
    """Valida una clasificación estructurada contra ``ESTRUCTURA_FALLAS``.

    Función pura (sin DB) → testeable. Retorna ``(ok, error)``.
    - ``categoria_codigo`` debe existir.
    - Para categorías de opción/equipo, ``subtipo_codigo`` debe pertenecer a sus opciones.
    - Para inversores, los códigos de tipo de falla deben ser válidos (≥1).
    """
    if not categoria_codigo:
        return False, "categoria_codigo requerido"
    cat = get_categoria(categoria_codigo)
    if not cat:
        return False, f"categoría desconocida: {categoria_codigo}"

    if cat["tipo"] in ("opcion", "equipo"):
        if not subtipo_codigo:
            return False, "subtipo_codigo requerido"
        if get_opcion(categoria_codigo, subtipo_codigo) is None:
            return False, f"opción inválida '{subtipo_codigo}' para '{categoria_codigo}'"
        return True, None

    if cat["tipo"] == "inversores":
        tipos = inversores_tipos or []
        if not tipos:
            return False, "debe indicar al menos un tipo de falla de inversor"
        invalidos = [t for t in tipos if t not in INVERSOR_TIPO_FALLA_CODIGOS]
        if invalidos:
            return False, f"tipos de falla de inversor inválidos: {invalidos}"
        return True, None

    return True, None


def es_subtipo_pendiente(categoria_codigo: str | None, subtipo_codigo: str | None) -> bool:
    """True si la opción marca ``pendiente_reclasificar`` (p.ej. desconexión sin identificar)."""
    if not categoria_codigo or not subtipo_codigo:
        return False
    op = get_opcion(categoria_codigo, subtipo_codigo)
    return bool(op and op.get("pendiente_reclasificar"))


def requiere_detalle(categoria_codigo: str | None, subtipo_codigo: str | None) -> bool:
    """True si la opción exige texto libre (mantenimiento de red, evento 'otro')."""
    if not categoria_codigo or not subtipo_codigo:
        return False
    op = get_opcion(categoria_codigo, subtipo_codigo)
    return bool(op and op.get("requiere_detalle"))


class EquipoFalla(StrEnum):
    """El equipo físico afectado. Se deduce de la clasificación: no se pregunta
    dos veces lo que ya dice el sistema afectado."""

    INVERSOR = "inversor"
    SMARTLOGGER = "smartlogger"
    TRACKER = "tracker"
    STRING = "string"
    MEDIDOR = "medidor"
    ESTACION_METEOROLOGICA = "estacion_meteorologica"


_MEDIDORES_FRONTERA = {"medidor_principal", "medidor_respaldo"}
_CODIGOS_EQUIPO = {e.value for e in EquipoFalla}


def equipo_de(categoria_codigo: str | None, subtipo_codigo: str | None) -> EquipoFalla | None:
    """El equipo afectado según la clasificación, o `None` si no es un equipo."""
    if categoria_codigo == "inversores":
        return EquipoFalla.INVERSOR
    if categoria_codigo == "frontera" and subtipo_codigo in _MEDIDORES_FRONTERA:
        return EquipoFalla.MEDIDOR
    if categoria_codigo == "equipos" and subtipo_codigo in _CODIGOS_EQUIPO:
        return EquipoFalla(subtipo_codigo)
    return None
