"""Genera `apps/<dominio>/models.py` desde los metadatos de SQLAlchemy.

Por que un generador y no transcripcion a mano: son 118 tablas y ~1200 columnas.
Transcribirlas es garantizar erratas silenciosas — un `nullable` invertido o un
`max_length` de menos no falla al arrancar, falla el dia que alguien guarda un
valor largo. Los metadatos de `Base.metadata` ya tienen tipo, nulabilidad,
claves foraneas, indices y restricciones unicas exactos; leerlos es mas barato y
mas fiable.

Lo normal seria `manage.py inspectdb`, pero exige una base viva y el .env local
no apunta a ninguna (`CLAUDE.md`). Los metadatos son la mejor fuente disponible.

**Lo generado es un borrador, no el resultado final.** El generador acierta la
estructura; no puede inventar lo que no esta en el esquema:

  - `verbose_name` en español (queda el nombre de la columna)
  - `TextChoices` para las columnas de estado que se guardan como texto
  - los docstrings que explican el modelo de datos
  - `related_name` con sentido cuando hay varias FK a la misma tabla

Uso:
    PYTHONPATH=. uv run python scripts/generar_modelos_django.py <dominio>...
    PYTHONPATH=. uv run python scripts/generar_modelos_django.py --listar
"""

import os
import sys
from collections import defaultdict

import app.models  # noqa: F401  — registra todas las tablas en Base.metadata
from app.models.base import Base

# Reparto de tablas por app, segun docs/DOMINIOS.md. Una tabla en dos dominios
# es un error de este mapa, no del generador: `verificar()` lo detecta.
DOMINIOS: dict[str, list[str]] = {
    "plataforma": ["usuarios", "notificaciones", "informes_guardados"],
    "proyectos": [
        "proyectos", "portafolios", "proyecto_info_tecnica", "proyecto_inversores",
        "proyecto_inversionistas", "proyectos_pendientes_ignorados", "gestion_registros",
        "costos_variables", "verificacion_costos", "promotor_catalogo_requisitos",
        "promotor_seguimientos", "generacion_diaria",
    ],
    "clientes": [
        "clientes", "cliente_documentos_comerciales", "cliente_tasa_servicio",
        "contactos", "proyecto_area_contacto",
    ],
    "fronteras": [
        "fronteras", "fronteras_quoia_ignoradas", "contrato_frontera",
        "operadores_red", "operadores_red_contactos",
    ],
    "comercial": [
        "oportunidades", "oportunidad_estado_historial", "oportunidad_gestiones",
        "oportunidad_ofertas", "oportunidad_oferta_proyectos",
    ],
    "contratos": ["contratos_servicio", "pagos_servicio", "polizas"],
    "ppa": [
        "ppa_contratos", "ppa_responsables", "ppa_tarifas",
        "ppa_compromisos_energia", "ipp_mensual", "ppa_contrato_proyectos",
    ],
    "facturacion": [
        "factura_agrupacion", "factura_orden", "factura_emitida", "contrato_factura",
    ],
    "mercado_xm": [
        "despacho_contrato_dia", "despacho_contrato_mensual", "precio_bolsa_mensual",
        "asic_solicitudes", "asic_cambios_contratos", "gescon_diccionario_contratos",
        "cumplimiento_mensual", "clasificacion_energia_mensual", "rec_procesos",
    ],
    "liquidaciones": ["liquidaciones", "liquidacion_costos", "liquidacion_facturas"],
    "registros_cnd": [
        "registro_conexion", "registro_etapa", "registro_transicion", "registro_hito",
        "registro_parametros_93", "registro_equipo_frontera", "registro_documento",
        "registro_alerta",
    ],
    "energia": [
        "reporte_energia_generacion", "reporte_energia_exclusiones",
        "reporte_energia_consumo",
    ],
    "monitoreo": [
        "fallas_cat_categorias", "fallas_cat_tipos", "fallas_cat_estados",
        "fallas_cat_prioridades", "fallas_cat_resoluciones", "fallas",
        "fallas_seguimientos", "fallas_intervalos", "falla_inversores", "alertas",
        # `mantenimientos` y `mantenimiento_impacto` salieron de aca el
        # 2026-09-07: se eliminaron de la base (migraciones 0005 y 0006 de
        # monitoreo). Si vuelven a la lista, este generador les vuelve a crear
        # el modelo.
        "starlink_facturas",
        "starlink_mapeo_sitio", "starlink_factura_linea", "alarmas_monitoreo",
    ],
    "om": [
        "om_ipc_tasas", "om_seleccion_mensual", "om_factura_mensual",
        "om_pagina_sin_match", "om_documento_proyecto", "proyecto_informe_om",
    ],
    "arriendos": [
        "arr_proyectos", "arr_arrendador", "arr_ipc_tasas", "arr_documento",
        "arr_seleccion_mensual",
    ],
    "contabilidad": [
        "panel_contable", "panel_contable_linea", "clasificacion_liquidacion",
        "mapeo_celda_concepto", "alias_fuente_ingreso", "panel_soporte",
        "panel_consecutivo",
    ],
    "mandatos": [
        "mandatos", "mandato_inversionistas", "mandato_correos", "finanzas_mandatos",
        "liquidacion_mandatos", "liquidacion_mandato_lineas",
    ],
    "garantias": [
        "gar_calculo", "gar_componente_real", "gar_componente_pred", "xm_archivo",
        "xm_medida", "garantias_ajustes", "garantia_snapshot", "garantia_pagado",
        "balcttos_neto",
    ],
    "retos": ["retos_trimestre", "retos_metrica", "retos_valor_semanal"],
}

TABLA_A_DOMINIO = {t: d for d, ts in DOMINIOS.items() for t in ts}

# La app que define `Timer`: las demas lo importan de ahi.
APP_BASE = "plataforma"

TIMESTAMPS = ("created_at", "updated_at")

ON_DELETE = {
    "CASCADE": "models.CASCADE",
    "SET NULL": "models.SET_NULL",
    "RESTRICT": "models.PROTECT",
    "NO ACTION": "models.DO_NOTHING",
}


# Terminaciones de plural en español cuyo singular NO se obtiene quitando la "s".
# Ojo: NO existe una regla para "-les". "papeles"->"papel" y
# "variables"->"variable" acaban igual y salen de singulares distintos; eso es un
# problema de diccionario, no de reglas. Por eso el caso general es quitar solo
# la "s" y lo demás va en CLASES_A_MANO.
_PLURALES = (("ones", "on"), ("ades", "ad"), ("udes", "ud"), ("res", "r"),
             ("nes", "n"), ("ces", "z"))


def singular(palabra: str) -> str:
    """`operadores` -> `operador`, `liquidaciones` -> `liquidacion`.

    Se singulariza cada token del nombre de tabla, no solo el último, porque así
    los nombres coinciden con los de los modelos SQLAlchemy que ya existen
    (`fallas_seguimientos` -> `FallaSeguimiento`) y nadie tiene que aprender dos
    nombres para la misma tabla.
    """
    for fin, reemplazo in _PLURALES:
        if palabra.endswith(fin) and len(palabra) > len(fin) + 1:
            return palabra[: -len(fin)] + reemplazo
    if palabra.endswith("s") and len(palabra) > 2:
        return palabra[:-1]
    return palabra


# Tablas cuyo nombre de clase no sale de la regla. Se declaran a mano.
CLASES_A_MANO: dict[str, str] = {
    "ipp_mensual": "IppMensual",
    "rec_procesos": "RecProceso",
    "alias_fuente_ingreso": "AliasFuenteIngreso",   # "alias" es invariable
    "finanzas_mandatos": "FinanzasMandato",
    "balcttos_neto": "BalcttosNeto",                # abreviatura, no plural
    "registro_parametros_93": "RegistroParametros93",  # "9.3" es el numeral de la CREG
    # "comerciales" es plural de "comercial": la regla genérica corta la "s" y
    # deja "comerciale". Es un problema de diccionario, no de regla (ver el
    # comentario de _PLURALES).
    "cliente_documentos_comerciales": "ClienteDocumentoComercial",
}

# `related_name` legibles. El generador emite `<tabla>_por_<columna>`, que es
# único pero ilegible; acá se declaran los de las relaciones que algún recurso
# recorre de verdad. **Va en el generador y no editado a mano en models.py**
# porque regenerar un dominio pisa el archivo entero: un ajuste que no esté
# aquí se pierde en la siguiente corrida.
RELACIONES_A_MANO: dict[tuple[str, str], str] = {
    ("polizas", "proyecto_id"): "polizas",
    ("fronteras", "proyecto_id"): "fronteras",
    ("proyecto_info_tecnica", "proyecto_id"): "info_tecnica",
    ("notificaciones", "usuario_id"): "notificaciones",
    ("verificacion_costos", "proyecto_id"): "verificacion_costos",
    ("operadores_red_contactos", "operador_red_id"): "contactos",
    ("proyecto_inversionistas", "proyecto_id"): "inversionistas",
    ("proyectos", "portafolio_id"): "proyectos",
    ("retos_metrica", "reto_id"): "metricas",
    ("retos_valor_semanal", "metrica_id"): "valores",
    ("starlink_factura_linea", "factura_id"): "lineas",
    ("ppa_tarifas", "contrato_id"): "tarifas",
    ("ppa_compromisos_energia", "contrato_id"): "compromisos",
    ("ppa_contrato_proyectos", "contrato_id"): "proyectos_vinculados",
    ("ppa_contratos", "responsable_id"): "contratos",
    ("cliente_documentos_comerciales", "ppa_contrato_id"): "documentos_comerciales",
    ("panel_contable_linea", "panel_id"): "lineas",
    ("liquidacion_costos", "liquidacion_id"): "costos",
    ("liquidacion_facturas", "liquidacion_id"): "facturas",
    ("liquidacion_mandatos", "liquidacion_id"): "mandatos",
    ("liquidacion_mandato_lineas", "mandato_id"): "lineas",
    ("registro_conexion", "proyecto_id"): "registro_conexion",
    ("registro_etapa", "registro_id"): "etapas",
    ("registro_transicion", "etapa_id"): "transiciones",
    ("registro_hito", "registro_id"): "hitos",
    ("registro_parametros_93", "registro_id"): "parametros_93",
    ("registro_equipo_frontera", "registro_id"): "equipos",
    ("registro_documento", "registro_id"): "documentos",
    ("registro_alerta", "registro_id"): "alertas",
    ("gar_componente_real", "calculo_id"): "reales",
    ("gar_componente_pred", "calculo_id"): "predicciones",
    ("cliente_documentos_comerciales", "cliente_id"): "documentos_comerciales",
    ("contactos", "cliente_id"): "contactos",
    ("cliente_tasa_servicio", "cliente_id"): "tasas_servicio",
    ("fallas_seguimientos", "falla_id"): "seguimientos",
    ("fallas_intervalos", "falla_id"): "intervalos",
    ("falla_inversores", "falla_id"): "inversores_afectados",
    ("fallas_cat_tipos", "categoria_id"): "tipos",
    ("proyecto_inversores", "proyecto_id"): "inversores",
    ("oportunidad_oferta_proyectos", "oferta_id"): "proyectos_declarados",
    ("oportunidad_ofertas", "oportunidad_id"): "ofertas",
    ("oportunidad_gestiones", "oportunidad_id"): "gestiones",
    ("proyecto_area_contacto", "proyecto_id"): "area_contactos",
    ("proyecto_inversionistas", "proyecto_id"): "inversionistas",
    ("ppa_contrato_proyectos", "proyecto_id"): "contratos_ppa",
}


def nombre_clase(tabla: str) -> str:
    """`retos_valor_semanal` -> `RetoValorSemanal`."""
    if tabla in CLASES_A_MANO:
        return CLASES_A_MANO[tabla]
    return "".join(singular(p).capitalize() for p in tabla.split("_"))


def _default_de_python(col, base: str) -> str | None:
    """El `default=` de Python de SQLAlchemy (el que NO está en la base).

    71 columnas lo tenían. SQLAlchemy lo aplica al construir el objeto; Django,
    sin equivalente declarado, manda NULL. Mismo fallo que con `server_default`:
    la fila revienta si la columna es NOT NULL.
    """
    d = col.default
    arg = getattr(d, "arg", None)
    if d is None or arg is None:
        return None
    if callable(arg):
        # Un `lambda` no se puede traducir; los dos casos reales del repo son
        # una fecha de cálculo y una lista vacía.
        if arg is list:
            return "list"
        if arg is dict:
            return "dict"
        return "timezone.now" if base == "DateTimeField" else None
    if base == "BooleanField":
        return "True" if arg else "False"
    if base in ("IntegerField", "BigIntegerField", "FloatField", "DecimalField"):
        return repr(arg) if isinstance(arg, (int, float)) else None
    if base in ("CharField", "TextField") and isinstance(arg, str):
        return f'"{arg}"'
    return None


def valor_por_defecto(col, base: str) -> str | None:
    """Traduce el `server_default` de SQLAlchemy a un `default=` de Django.

    No es cosmetico: Django manda TODAS las columnas en el INSERT, asi que un
    default que solo vive en PostgreSQL nunca llega a usarse — la columna viaja
    como NULL y la fila revienta si es NOT NULL (paso con
    `registro_etapa.fecha_estado`). SQLAlchemy no lo sufria porque omite del
    INSERT las columnas sin valor.
    """
    sd = col.server_default
    if sd is None or getattr(sd, "arg", None) is None:
        return _default_de_python(col, base)
    texto = str(sd.arg).strip()
    if texto.lower() in ("now()", "current_timestamp"):
        return "timezone.now"
    if base == "BooleanField":
        return {"true": "True", "false": "False"}.get(texto.lower())
    literal = texto.strip("'")
    if base in ("IntegerField", "BigIntegerField", "FloatField", "DecimalField"):
        try:
            float(literal)
        except ValueError:
            return None
        return literal
    if base in ("CharField", "TextField"):
        return f'"{literal}"' if literal and "::" not in texto else None
    return None


def campo_django(col) -> str:
    """Traduce una columna de SQLAlchemy al campo Django equivalente."""
    tipo = type(col.type).__name__
    args: list[str] = []

    if col.primary_key:
        base = "BigAutoField" if tipo in ("BigInteger", "Integer") else "CharField"
        args.append("primary_key=True")
        if base == "CharField":
            args.append(f"max_length={getattr(col.type, 'length', None) or 255}")
        return f"models.{base}(" + ", ".join(args) + ")"

    if tipo in ("String", "Unicode", "VARCHAR"):
        base = "CharField"
        args.append(f"max_length={getattr(col.type, 'length', None) or 255}")
    elif tipo in ("Text", "UnicodeText"):
        base = "TextField"
    elif tipo == "BigInteger":
        base = "BigIntegerField"
    elif tipo in ("Integer", "SmallInteger"):
        base = "IntegerField"
    elif tipo == "Boolean":
        base = "BooleanField"
    elif tipo == "DateTime":
        base = "DateTimeField"
    elif tipo == "Date":
        base = "DateField"
    elif tipo == "Time":
        base = "TimeField"
    elif tipo == "Numeric":
        base = "DecimalField"
        args.append(f"max_digits={col.type.precision or 20}")
        args.append(f"decimal_places={col.type.scale or 2}")
    elif tipo in ("Float", "REAL", "DOUBLE_PRECISION"):
        base = "FloatField"
    elif tipo in ("JSON", "JSONB"):
        base = "JSONField"
    elif tipo == "ARRAY":
        # ArrayField necesita el tipo interno; se marca para revisar a mano.
        return "ArrayField(models.TextField())  # TODO revisar el tipo interno"
    elif tipo == "LargeBinary":
        base = "BinaryField"
    elif tipo in ("Uuid", "UUID"):
        base = "UUIDField"
    elif tipo == "Enum":
        # El enum vive en PostgreSQL; Django lo lee como texto y valida con
        # `choices`. Se emiten los valores para que queden a la vista.
        base = "CharField"
        largo = max((len(v) for v in col.type.enums), default=50)
        args.append(f"max_length={largo}")
        args.append("choices=[" + ", ".join(f'("{v}", "{v}")' for v in col.type.enums) + "]")
    else:
        return f"models.TextField()  # TODO tipo sin traducir: {tipo}"

    if col.nullable:
        args.extend(["null=True", "blank=True"])
    if col.index:
        args.append("db_index=True")
    predeterminado = valor_por_defecto(col, base)
    if predeterminado is not None:
        args.append(f"default={predeterminado}")
    return f"models.{base}(" + ", ".join(args) + ")"


def campo_fk(col, dominio: str, importes: set[str]) -> str | None:
    """Traduce una FK, resolviendo si el destino vive en otra app."""
    fks = list(col.foreign_keys)
    if not fks:
        return None
    destino_tabla = fks[0].column.table.name
    destino_dom = TABLA_A_DOMINIO.get(destino_tabla)
    if destino_dom is None:
        return None                       # tabla fuera del mapa: se deja como entero

    clase = nombre_clase(destino_tabla)
    if destino_dom != dominio:
        importes.add(destino_dom)
        referencia = f'"{destino_dom}.{clase}"'
    else:
        referencia = f'"{clase}"'

    ondelete = ON_DELETE.get((fks[0].ondelete or "").upper(), "models.DO_NOTHING")
    args = [referencia, f"on_delete={ondelete}", f'db_column="{col.name}"']
    if col.nullable:
        args.extend(["null=True", "blank=True"])
    # `related_name` explicito: sin el, dos FK a la misma tabla chocan.
    legible = RELACIONES_A_MANO.get((col.table.name, col.name))
    args.append(f'related_name="{legible or f"{col.table.name}_por_{col.name}"}"')
    return "models.ForeignKey(" + ", ".join(args) + ")"


def generar(dominio: str) -> str:
    md = Base.metadata
    tablas = [md.tables[t] for t in DOMINIOS[dominio] if t in md.tables]
    faltantes = [t for t in DOMINIOS[dominio] if t not in md.tables]

    importes_apps: set[str] = set()
    cuerpos: list[str] = []

    for tabla in tablas:
        tiene_timer = all(c in tabla.columns for c in TIMESTAMPS)
        base = "Timer" if tiene_timer else "models.Model"
        lineas = [f"class {nombre_clase(tabla.name)}({base}):"]

        for col in tabla.columns:
            if tiene_timer and col.name in TIMESTAMPS:
                continue                  # los aporta Timer
            atributo = col.name
            declaracion = campo_fk(col, dominio, importes_apps)
            if declaracion is not None:
                atributo = col.name[:-3] if col.name.endswith("_id") else col.name
            else:
                declaracion = campo_django(col)
            lineas.append(f"    {atributo} = {declaracion}")

        pks = [c.name for c in tabla.columns if c.primary_key]
        if len(pks) > 1:
            # Clave primaria compuesta (tabla de asociación sin `id`). Sin esto
            # Django inventa un `id` implícito y toda consulta pide una columna
            # que no existe: `SELECT id FROM ppa_contrato_proyectos` → error.
            campos = ", ".join(f'"{c}"' for c in pks)
            lineas.append(f"    pk = models.CompositePrimaryKey({campos})")

        lineas.append("")
        lineas.append("    class Meta:")
        lineas.append("        managed = False        # el esquema lo posee Alembic")
        lineas.append(f'        db_table = "{tabla.name}"')

        unicas = [
            sorted(c.name for c in con.columns)
            for con in tabla.constraints
            if type(con).__name__ == "UniqueConstraint"
        ]
        for cols in unicas:
            nombres = [c[:-3] if c.endswith("_id") and
                       list(tabla.columns[c].foreign_keys) else c for c in cols]
            # La coma final importa: `("a")` es la cadena "a", no una tupla.
            interior = ", ".join(f'"{n}"' for n in nombres)
            if len(nombres) == 1:
                interior += ","
            lineas.append(f"        unique_together = [({interior})]")
        cuerpos.append("\n".join(lineas))

    cabecera = [
        f'"""Modelos del dominio `{dominio}`.',
        "",
        "GENERADO por scripts/generar_modelos_django.py desde los metadatos de",
        "SQLAlchemy. Es un BORRADOR: falta el verbose_name en español, los",
        "TextChoices de las columnas de estado y los docstrings que explican el",
        "modelo de datos. Revisar antes de portar la API del recurso.",
        "",
        "`managed = False` en todos: mientras FastAPI siga leyendo estas tablas,",
        "el único dueño del esquema es Alembic (ver apps/README.md).",
        '"""',
        "",
        "from django.db import models",
        "",
    ]
    if any("timezone.now" in c for c in cuerpos):
        cabecera.insert(-1, "from django.utils import timezone")
    if dominio == APP_BASE:
        cabecera.append("")
    else:
        cabecera.append(f"from apps.{APP_BASE}.models import Timer")
        cabecera.append("")
    if faltantes:
        cabecera.append(f"# TODO tablas del mapa que no están en los metadatos: {faltantes}")
        cabecera.append("")

    return "\n".join(cabecera) + "\n" + "\n\n\n".join(cuerpos) + "\n"


def verificar() -> list[str]:
    """Comprueba que el mapa cubre las tablas y que ninguna está repetida."""
    problemas = []
    vistas = defaultdict(list)
    for dom, tablas in DOMINIOS.items():
        for t in tablas:
            vistas[t].append(dom)
    for t, doms in vistas.items():
        if len(doms) > 1:
            problemas.append(f"{t} aparece en {doms}")

    en_metadata = set(Base.metadata.tables)
    sin_mapear = sorted(en_metadata - set(TABLA_A_DOMINIO))
    if sin_mapear:
        problemas.append(f"{len(sin_mapear)} tablas sin dominio: {sin_mapear}")
    return problemas


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "--listar":
        for p in verificar():
            print("AVISO:", p)
        print(f"\n{len(DOMINIOS)} dominios:")
        for d, ts in DOMINIOS.items():
            print(f"  {d:16} {len(ts):3} tablas")
        sys.exit(0)

    for dominio in args:
        if dominio not in DOMINIOS:
            sys.exit(f"dominio desconocido: {dominio}")
        destino = f"apps/{dominio}/models.py"
        # Solo se pisa lo que este generador escribio. `plataforma/models.py` se
        # mantiene a mano (Timer, Rol) y una regeneracion lo borro entero una vez:
        # la marca del docstring es lo que lo distingue.
        if os.path.exists(destino) and "GENERADO por" not in open(destino, encoding="utf-8").read(600):
            print(f"OMITIDO {destino}: escrito a mano, no lleva la marca 'GENERADO por'")
            continue
        with open(destino, "w", encoding="utf-8") as fh:
            fh.write(generar(dominio))
        print(f"escrito {destino}")
