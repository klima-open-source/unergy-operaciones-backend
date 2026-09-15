"""Balance mensual de energía en bolsa (MWh).

Responde la pregunta operativa: **este mes, ¿cuánto compro o compraré en
bolsa?** — la cifra que gobierna garantías y cargos regulatorios.

Libro mayor (reglas confirmadas con Juan, 2026-07-26):

    UNGG   Venta en bolsa (e)                 +X   cargos regulatorios altos
           Compras en bolsa
              · Directas — duplicados (c)     −Z   generan garantías
              · No directas — uso del recurso −W   sin garantía
           NETO UNGG                          ±N
    UNGC   Venta en bolsa (f)                 +Y   solo cartera

El neteo ocurre DENTRO del mismo agente: (f) es de UNGC y no se contrarresta
contra (c), que es de UNGG.

Este módulo NO reimplementa la lógica de GESCON: consume el payload de
`/cumplimiento/plantas-contratos` (que ya resuelve vigencias, relevos,
duplicados y uso del recurso) y le añade la única pieza que faltaba —
**cruzar los días con los porcentajes de despacho**.

Ese cruce es el corazón de la feature. Hasta ahora el módulo tenía dos
cálculos de bolsa que no coincidían: `plantas-contratos` repartía por DÍAS
(una planta con contrato vigente todo el mes aportaba 0 a bolsa, sin importar
su % de despacho) y `energia-transada` repartía por PORCENTAJE (sin ver los
tramos). Aquí ambos ejes se tratan a la vez: para cada tramo del mes se conoce
el Σ% contratado, y todo lo que sobra se vende en bolsa.

Puerto de `app/services/balance_energia.py`. Cierra la última escotilla a
SQLAlchemy que quedaba en `apps/` (ver `apps/garantias/services/proyecciones.py`,
que lo consumía por import diferido).

Solo cambiaron las cuatro funciones que tocaban la sesión —`_nombre_frontera`,
`calcular_balance`, `_plantas_contratos_de`, `_tasa_diaria_reciente`—; el resto
(los tramos, la agregación, el inventario) es aritmética pura y vino verbatim.
`hoy` pasa a resolverse con `hoy_col()`: el contenedor corre en UTC y el corte de
un balance no puede caer en el mes siguiente por la diferencia horaria (CLAUDE.md).
"""
import calendar
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from apps.plataforma.services.fechas import hoy_col

logger = logging.getLogger("operaciones.cumplimiento")

# Tolerancia para comparar porcentajes acumulados (evita que 0.5 + 0.5 = 1.0000001
# se reporte como anomalía por el error de coma flotante).
_EPS = 1e-6


def _iso_a_fecha(v):
    """'2026-07-24' | date | None -> date | None."""
    if not v:
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _segmento(entry: dict, first_day: date, last_day: date):
    """Ventana del mes que ocupa una fila de planta, recortada al mes.

    `plantas-contratos` ya estampa `segmento_inicio`/`segmento_fin` en cada fila
    (helper `_con_segmento`). Se cae a fecha_inicio/fecha_fin y, en último
    término, al mes completo, para tolerar payloads viejos.
    """
    ini = _iso_a_fecha(entry.get("segmento_inicio")) or _iso_a_fecha(entry.get("fecha_inicio")) or first_day
    fin = _iso_a_fecha(entry.get("segmento_fin")) or _iso_a_fecha(entry.get("fecha_fin")) or last_day
    ini = max(ini, first_day)
    fin = min(fin, last_day)
    return (ini, fin) if ini <= fin else (None, None)


def _pct(valor):
    """% de despacho normalizado a [0, 1]. Devuelve (usable, crudo).

    Se almacena 0-1 y se muestra ×100. Hay filas corruptas en producción con
    valores > 1 (un formulario viejo guardaba el porcentaje ya multiplicado):
    se clampan para no inventar energía y se reportan como anomalía.
    """
    crudo = float(valor or 0)
    return min(max(crudo, 0.0), 1.0), crudo


def construir_tramos(data: dict, first_day: date, last_day: date) -> dict:
    """Corta el mes de cada planta en tramos con reparto porcentual constante.

    FUNCIÓN PURA: sin BD, sin red, sin reloj. Es donde vive toda la aritmética
    del balance, y por eso es la que está cubierta por tests.

    Toma el payload de `get_plantas_contratos` y devuelve, por planta, los
    tramos elementales del mes. Se corta en los bordes de todas las
    asignaciones y de todos los segmentos de bolsa, así que dentro de un tramo
    el reparto no cambia y cada asignación lo cubre entero o no lo toca.

    Por tramo:
      pct_ppa   Σ% de asignaciones NO duplicadas (incluye uso del recurso: esa
                energía sí se entrega al contrato).
      pct_dup   Σ% de duplicados → compra DIRECTA en bolsa (energía que no
                existe y hay que comprar).
      pct_uso   Σ% de uso del recurso → compra NO DIRECTA (la energía existe;
                se le paga al dueño a precio de bolsa). Es un subconjunto de
                pct_ppa: la planta clasifica doble (a + c).
      pct_venta_bolsa  1 − pct_ppa. Lo que no está contratado se vende en bolsa.
      piscina_venta    'ungc' si un segmento de bolsa comercializador cubre el
                tramo; 'ungg' en cualquier otro caso — incluido el remanente
                porcentual de una planta bajo contrato, que va a bolsa directa
                por UNGG.
    """
    asignaciones: dict[int, list] = defaultdict(list)
    segmentos_bolsa: dict[int, list] = defaultdict(list)
    nombres: dict[int, str] = {}
    anomalias: list[dict] = []

    for contrato in data.get("venta") or []:
        for p in contrato.get("plantas") or []:
            pid = p.get("id")
            if pid is None:
                continue
            nombres.setdefault(pid, p.get("nombre") or f"Proyecto {pid}")
            ini, fin = _segmento(p, first_day, last_day)
            if ini is None:
                continue
            pct, crudo = _pct(p.get("pct_despacho"))
            if crudo > 1.0 + _EPS:
                anomalias.append({
                    "proyecto_id": pid,
                    "planta": nombres[pid],
                    "contrato": contrato.get("nombre"),
                    "pct_crudo": crudo,
                    "motivo": "porcentaje_despacho > 1 (se clampó a 100%)",
                })
            asignaciones[pid].append({
                "ini": ini,
                "fin": fin,
                "pct": pct,
                "es_duplicado": bool(p.get("es_duplicado")),
                "uso_del_recurso": bool(p.get("uso_del_recurso")),
                "contrato": contrato.get("nombre"),
                "contrato_id": contrato.get("id"),
                "comprador": contrato.get("comprador_nombre"),
                "codigo_sic": p.get("codigo_sic"),
            })

    for p in data.get("bolsa") or []:
        pid = p.get("id")
        if pid is None:
            continue
        nombres.setdefault(pid, p.get("nombre") or f"Proyecto {pid}")
        ini, fin = _segmento(p, first_day, last_day)
        if ini is None:
            continue
        segmentos_bolsa[pid].append({
            "ini": ini,
            "fin": fin,
            "piscina": p.get("piscina") or "libre",
            "codigo_sic": p.get("codigo_sic"),
        })

    un_dia = timedelta(days=1)
    plantas: dict[int, dict] = {}

    for pid in sorted(set(asignaciones) | set(segmentos_bolsa), key=lambda i: nombres.get(i, "")):
        asigs = asignaciones.get(pid) or []
        bolsas = segmentos_bolsa.get(pid) or []

        cortes = {first_day, last_day + un_dia}
        for v in asigs + bolsas:
            cortes.add(v["ini"])
            cortes.add(v["fin"] + un_dia)
        cortes = sorted(c for c in cortes if first_day <= c <= last_day + un_dia)

        tramos = []
        for inicio, siguiente in zip(cortes, cortes[1:]):
            fin = siguiente - un_dia
            if fin < inicio:
                continue
            activos = [a for a in asigs if a["ini"] <= inicio and a["fin"] >= fin]
            pct_ppa_bruto = sum(a["pct"] for a in activos if not a["es_duplicado"])
            pct_dup = sum(a["pct"] for a in activos if a["es_duplicado"])
            pct_uso = sum(a["pct"] for a in activos if a["uso_del_recurso"])
            pct_ppa = min(1.0, pct_ppa_bruto)
            if pct_ppa_bruto > 1.0 + _EPS:
                anomalias.append({
                    "proyecto_id": pid,
                    "planta": nombres[pid],
                    "contrato": ", ".join(
                        str(a["contrato"]) for a in activos if not a["es_duplicado"]
                    ),
                    "pct_crudo": round(pct_ppa_bruto, 4),
                    "motivo": "la suma de despachos del tramo supera el 100%",
                })
            seg_bolsa = next(
                (b for b in bolsas if b["ini"] <= inicio and b["fin"] >= fin), None
            )
            es_comercializador = bool(seg_bolsa) and seg_bolsa["piscina"] == "comercializador"
            # Un tramo sin asignación NI segmento de bolsa cae FUERA de la ventana
            # operativa de la planta: `plantas-contratos` solo emite residuo de
            # bolsa entre fecha_inicio_comercializacion y fecha_fin_representacion.
            # Sin este corte, una planta que arranca el 10 vendería "el 100% en
            # bolsa" del 1 al 9, cuando todavía no existía.
            operativo = bool(activos) or bool(seg_bolsa)
            tramos.append({
                "ini": inicio,
                "fin": fin,
                "dias": (fin - inicio).days + 1,
                "pct_ppa": round(pct_ppa, 6),
                "pct_dup": round(min(pct_dup, 1.0), 6),
                "pct_uso": round(min(pct_uso, 1.0), 6),
                "pct_venta_bolsa": round(max(0.0, 1.0 - pct_ppa), 6) if operativo else 0.0,
                "piscina_venta": "ungc" if es_comercializador else "ungg",
                "codigo_sic_bolsa": seg_bolsa["codigo_sic"] if seg_bolsa else None,
                "operativo": operativo,
                "asignaciones": activos,
            })

        plantas[pid] = {"id": pid, "nombre": nombres[pid], "tramos": tramos}

    return {"plantas": plantas, "anomalias": anomalias}


# ── Agregación ────────────────────────────────────────────────────────────────

def _celda():
    return {"real": 0.0, "proyectado": 0.0, "total": 0.0, "n_plantas": 0}


def _sumar(celda: dict, real: float, proyectado: float):
    celda["real"] += real
    celda["proyectado"] += proyectado
    celda["total"] += real + proyectado


def _redondear(celda: dict):
    for k in ("real", "proyectado", "total"):
        celda[k] = round(celda[k], 3)
    return celda


def agregar_balance(plantas: dict, energia: dict) -> dict:
    """Suma los tramos en el libro mayor.

    `energia` = {proyecto_id: {idx_tramo: (mwh_real, mwh_proyectado)}}. Se pasa
    aparte para que la agregación sea pura y testeable sin tocar la API de
    generación.
    """
    ungg_venta, ungg_dir, ungg_nodir = _celda(), _celda(), _celda()
    ungc_venta = _celda()
    aporta = {"ungg_venta": set(), "ungg_dir": set(), "ungg_nodir": set(), "ungc_venta": set()}

    for pid, planta in plantas.items():
        por_tramo = energia.get(pid) or {}
        for idx, t in enumerate(planta["tramos"]):
            real, proy = por_tramo.get(idx, (0.0, 0.0))
            if not real and not proy:
                continue
            if t["pct_venta_bolsa"] > 0:
                destino = ungc_venta if t["piscina_venta"] == "ungc" else ungg_venta
                clave = "ungc_venta" if t["piscina_venta"] == "ungc" else "ungg_venta"
                _sumar(destino, real * t["pct_venta_bolsa"], proy * t["pct_venta_bolsa"])
                aporta[clave].add(pid)
            if t["pct_dup"] > 0:
                _sumar(ungg_dir, real * t["pct_dup"], proy * t["pct_dup"])
                aporta["ungg_dir"].add(pid)
            if t["pct_uso"] > 0:
                _sumar(ungg_nodir, real * t["pct_uso"], proy * t["pct_uso"])
                aporta["ungg_nodir"].add(pid)

    for clave, celda in (("ungg_venta", ungg_venta), ("ungg_dir", ungg_dir),
                         ("ungg_nodir", ungg_nodir), ("ungc_venta", ungc_venta)):
        celda["n_plantas"] = len(aporta[clave])

    compra_total = {
        "real": ungg_dir["real"] + ungg_nodir["real"],
        "proyectado": ungg_dir["proyectado"] + ungg_nodir["proyectado"],
        "total": ungg_dir["total"] + ungg_nodir["total"],
        "n_plantas": len(aporta["ungg_dir"] | aporta["ungg_nodir"]),
    }
    neto = {
        "real": ungg_venta["real"] - compra_total["real"],
        "proyectado": ungg_venta["proyectado"] - compra_total["proyectado"],
        "total": ungg_venta["total"] - compra_total["total"],
        "n_plantas": len(aporta["ungg_venta"] | aporta["ungg_dir"] | aporta["ungg_nodir"]),
    }

    return {
        "ungg": {
            "venta_bolsa": _redondear(ungg_venta),
            "compra_bolsa_directa": _redondear(ungg_dir),
            "compra_bolsa_no_directa": _redondear(ungg_nodir),
            "compra_bolsa_total": _redondear(compra_total),
            "neto": _redondear(neto),
        },
        "ungc": {"venta_bolsa": _redondear(ungc_venta)},
    }


def _energia_proyectada(plantas: dict, tasa_diaria: dict, first_day, last_day) -> dict:
    """Energía por tramo para un mes 100% futuro: (real=0, proyectado=tasa×días_tramo).
    `tasa_diaria` = {proyecto_id: MWh/día}. Tramos que no necesitan energía → (0,0)."""
    energia: dict[int, dict] = {}
    for pid, planta in plantas.items():
        tasa = tasa_diaria.get(pid)
        por_tramo = {}
        for idx, t in enumerate(planta["tramos"]):
            if tasa is None or not _necesita_energia(t):
                por_tramo[idx] = (0.0, 0.0)
                continue
            ini = max(t["ini"], first_day)
            fin = min(t["fin"], last_day)
            dias = (fin - ini).days + 1 if fin >= ini else 0
            por_tramo[idx] = (0.0, tasa * dias)
        energia[pid] = por_tramo
    return energia


def construir_inventario(plantas: dict, energia: dict, fronteras: dict,
                         estimados: set | None = None) -> list:
    """Tabla plana: una fila por (planta, tramo, rol). Es el Excel de Juan,
    automatizado y con las fechas y los MWh que el Excel no tenía.

    Cada fila lleva también la generación BASE del tramo (`gen_tramo_*`), que es
    lo que el % multiplica. Sin ese dato el desglose no es auditable: se ve el
    aporte pero no de dónde sale.
    """
    estimados = estimados or set()
    filas = []
    for pid, planta in plantas.items():
        por_tramo = energia.get(pid) or {}
        frontera = fronteras.get(pid) or planta["nombre"]
        for idx, t in enumerate(planta["tramos"]):
            real, proy = por_tramo.get(idx, (None, None))
            sin_datos = real is None

            def _fila(**kw):
                pct = kw.pop("pct")
                base = {
                    "proyecto_id": pid,
                    "planta": planta["nombre"],
                    "frontera": frontera,
                    "desde": t["ini"].isoformat(),
                    "hasta": t["fin"].isoformat(),
                    "dias": t["dias"],
                    "pct": round(pct, 6),
                    # Generación del tramo antes de aplicar el %: el multiplicando.
                    "gen_tramo_real": None if sin_datos else round(real, 3),
                    "gen_tramo_proyectado": None if sin_datos else round(proy, 3),
                    "estimado": (pid, idx) in estimados,
                    "mwh_real": None if sin_datos else round(real * pct, 3),
                    "mwh_proyectado": None if sin_datos else round(proy * pct, 3),
                    "mwh_total": None if sin_datos else round((real + proy) * pct, 3),
                    "contrato": None,
                    "contrato_id": None,
                    "codigo_sic": None,
                }
                base.update(kw)
                return base

            for a in t["asignaciones"]:
                if a["es_duplicado"]:
                    filas.append(_fila(
                        categoria="c", metodo="Duplicado", pct=a["pct"],
                        estado=f"Duplicado en {a['contrato']}",
                        contrato=a["contrato"], contrato_id=a["contrato_id"],
                        codigo_sic=a["codigo_sic"],
                    ))
                elif a["uso_del_recurso"]:
                    filas.append(_fila(
                        categoria="uso", metodo="Uso del recurso", pct=a["pct"],
                        estado=f"Apoyando {a['contrato']}",
                        contrato=a["contrato"], contrato_id=a["contrato_id"],
                        codigo_sic=a["codigo_sic"],
                    ))
                else:
                    filas.append(_fila(
                        categoria="a", metodo="Registrado", pct=a["pct"],
                        estado=f"Registrado en {a['contrato']}",
                        contrato=a["contrato"], contrato_id=a["contrato_id"],
                        codigo_sic=a["codigo_sic"],
                    ))

            if t["pct_venta_bolsa"] > 0:
                ungc = t["piscina_venta"] == "ungc"
                filas.append(_fila(
                    categoria="f" if ungc else "e",
                    metodo="Venta en bolsa",
                    pct=t["pct_venta_bolsa"],
                    estado="En bolsa con UNGC" if ungc else "Libre en UNGG",
                    codigo_sic=t["codigo_sic_bolsa"],
                ))

    filas.sort(key=lambda f: (f["frontera"] or "", f["desde"], f["categoria"]))
    return filas


# ── Orquestación ──────────────────────────────────────────────────────────────

def _interseccion(a_ini: date, a_fin: date, b_ini: date, b_fin: date):
    ini, fin = max(a_ini, b_ini), min(a_fin, b_fin)
    return (ini, fin) if ini <= fin else None


def _necesita_energia(tramo: dict) -> bool:
    """Si el tramo no aporta a ninguna línea ni a ninguna fila del inventario,
    no vale la pena pedirle la generación a la API."""
    return bool(tramo["asignaciones"]) or tramo["pct_venta_bolsa"] > 0


def _nombre_frontera(ids: list) -> dict:
    """Nombre de frontera por proyecto, que es como Juan identifica las plantas.

    Una planta puede tener varias fronteras; se prefiere la de generación y
    activa. Sin frontera registrada, el llamador cae al nombre comercial.
    """
    if not ids:
        return {}
    from apps.fronteras.models import Frontera

    def _valor(v):
        return getattr(v, "value", v)

    def _orden(f):
        tipo = _valor(f.tipo_frontera)
        es_gen = tipo in ("generacion", "generacion_consumo")
        return (0 if es_gen else 1, 0 if _valor(f.estado) == "activa" else 1, f.id)

    salida: dict[int, str] = {}
    filas = Frontera.objects.filter(proyecto_id__in=ids)
    for f in sorted(filas, key=_orden):
        if f.proyecto_id is not None:
            salida.setdefault(f.proyecto_id, f.nombre_frontera)
    return salida


def calcular_balance(year: int, month: int,
                     excluir_compra_externa: bool = False, hoy: date | None = None,
                     incluir_todos: bool = False) -> dict:
    """Balance mensual de energía en bolsa, real + proyección al cierre.

    Real: generación de cada tramo dentro de [primer día, corte]. Si el tramo
    cubre toda esa ventana se usa la generación del mes (para el mes en curso
    la API solo tiene datos hasta hoy, así que ya es el real); si es parcial se
    pide el rango exacto y se suman deltas día a día — nunca regla de tres.

    Proyección: promedio diario real del propio mes × días que le quedan al
    tramo. Se usa el mes en curso en vez de una ventana de 60 días previos
    porque es la misma estación y evita ~55 consultas pesadas extra.
    """
    from apps.energia.services.comercializacion import identificador_monitoreo as _mon_id
    from apps.mercado_xm.services.cumplimiento.periodos import _fecha_corte
    from apps.mercado_xm.services.cumplimiento.piscinas import plantas_contratos
    from apps.mercado_xm.services.cumplimiento.xm_api import (
        _fetch_month, _fetch_range, _unergy_token,
    )
    from apps.proyectos.models import Proyecto

    hoy = hoy or hoy_col()
    total_dias = calendar.monthrange(year, month)[1]
    first_day = date(year, month, 1)
    last_day = date(year, month, total_dias)
    corte = _fecha_corte(year, month, hoy)
    es_mes_actual = (year, month) == (hoy.year, hoy.month)
    es_mes_futuro = (year, month) > (hoy.year, hoy.month)

    periodo = {
        "year": year, "month": month,
        "dias_mes": total_dias,
        "dia_corte": corte.day,
        "fecha_corte": corte.isoformat(),
        "es_mes_actual": es_mes_actual,
        "es_mes_futuro": es_mes_futuro,
    }
    vacio = {"real": 0.0, "proyectado": 0.0, "total": 0.0, "n_plantas": 0}
    if es_mes_futuro:
        return {
            "periodo": periodo,
            "balance": {
                "ungg": {k: dict(vacio) for k in (
                    "venta_bolsa", "compra_bolsa_directa", "compra_bolsa_no_directa",
                    "compra_bolsa_total", "neto")},
                "ungc": {"venta_bolsa": dict(vacio)},
            },
            "inventario": [],
            "advertencias": {"sin_datos": [], "pct_anomalos": [],
                             "compra_externa_en_bolsa": [], "tramos_estimados": []},
        }

    # El balance se arma sobre plantas-contratos, así que hereda de ahí el filtro
    # de empresa responsable (ver _contratos_vigentes en cumplimiento.py).
    data = plantas_contratos(year, month, incluir_todos=incluir_todos)

    ids_compra_externa = {
        p["id"] for c in (data.get("compra_externa") or [])
        for p in (c.get("plantas") or []) if p.get("id") is not None
    }

    derivado = construir_tramos(data, first_day, last_day)
    plantas = derivado["plantas"]
    if excluir_compra_externa:
        plantas = {pid: v for pid, v in plantas.items() if pid not in ids_compra_externa}

    # ── Generación ────────────────────────────────────────────────────────────
    sub_projects: dict[int, str] = {}
    if plantas:
        for p in Proyecto.objects.filter(id__in=list(plantas)):
            sp = _mon_id(p)
            if sp:
                sub_projects[p.id] = sp

    warning = None
    gen_mes: dict[int, float | None] = {}
    rangos: dict[tuple, float | None] = {}
    token = None
    if sub_projects:
        try:
            token = _unergy_token()
        except Exception as exc:
            logger.error("Auth Unergy failed in balance-energia: %s", exc)
            warning = "No se pudo autenticar con la API de generación: el balance sale sin MWh."

    if token:
        sps = sorted(set(sub_projects.values()))
        por_sp: dict[str, float | None] = {}
        with ThreadPoolExecutor(max_workers=min(len(sps), 12)) as pool:
            for sp, res in pool.map(lambda s: (s, _fetch_month(token, s, year, month)), sps):
                por_sp[sp] = res.get("mwh")
        gen_mes = {pid: por_sp.get(sp) for pid, sp in sub_projects.items()}

        # Tramos que solo cubren parte de la ventana real necesitan su rango exacto.
        necesarios = set()
        for pid, planta in plantas.items():
            sp = sub_projects.get(pid)
            if not sp:
                continue
            for t in planta["tramos"]:
                if not _necesita_energia(t):
                    continue
                ventana = _interseccion(t["ini"], t["fin"], first_day, corte)
                if ventana and ventana != (first_day, corte):
                    necesarios.add((sp, ventana[0], ventana[1]))
        if necesarios:
            tareas = list(necesarios)
            with ThreadPoolExecutor(max_workers=min(len(tareas), 12)) as pool:
                for tarea, res in pool.map(
                    lambda t: (t, _fetch_range(token, t[0], t[1], t[2])), tareas
                ):
                    rangos[tarea] = res.get("mwh")

    dias_reales = max(1, corte.day)
    energia: dict[int, dict] = {}
    sin_datos: list[dict] = []
    tramos_estimados: list[dict] = []
    claves_estimadas: set = set()

    for pid, planta in plantas.items():
        sp = sub_projects.get(pid)
        mwh_mes = gen_mes.get(pid)
        if mwh_mes is None:
            sin_datos.append({
                "proyecto_id": pid,
                "planta": planta["nombre"],
                "motivo": ("sin identificador de monitoreo" if not sp
                           else "sin lecturas de generación en el mes"),
            })
            continue
        promedio_diario = mwh_mes / dias_reales
        por_tramo = {}
        for idx, t in enumerate(planta["tramos"]):
            if not _necesita_energia(t):
                por_tramo[idx] = (0.0, 0.0)
                continue
            ventana = _interseccion(t["ini"], t["fin"], first_day, corte)
            if ventana is None:
                real = 0.0
            elif ventana == (first_day, corte):
                real = mwh_mes
            else:
                real = rangos.get((sp, ventana[0], ventana[1]))
                if real is None:
                    # Sin lecturas del rango exacto. Se estima con el promedio
                    # diario del mes y se declara: omitir esos días haría que el
                    # balance no cuadrara sin decir por qué.
                    dias = (ventana[1] - ventana[0]).days + 1
                    real = promedio_diario * dias
                    tramos_estimados.append({
                        "proyecto_id": pid, "planta": planta["nombre"],
                        "desde": ventana[0].isoformat(), "hasta": ventana[1].isoformat(),
                    })
                    claves_estimadas.add((pid, idx))
            futuro = _interseccion(t["ini"], t["fin"], corte + timedelta(days=1), last_day)
            dias_futuros = ((futuro[1] - futuro[0]).days + 1) if futuro else 0
            por_tramo[idx] = (real, promedio_diario * dias_futuros)
        energia[pid] = por_tramo

    fronteras = _nombre_frontera(list(plantas))
    balance = agregar_balance(plantas, energia)
    inventario = construir_inventario(plantas, energia, fronteras, claves_estimadas)

    # (g) Plantas externas: informativas. No entran al balance de bolsa porque su
    # energía está comprada por PPA fuera de GESCON.
    for c in data.get("compra_externa") or []:
        for p in c.get("plantas") or []:
            pid = p.get("id")
            filas_g = {
                "proyecto_id": pid,
                "planta": p.get("nombre"),
                "frontera": fronteras.get(pid) or p.get("nombre"),
                "categoria": "g",
                "metodo": "Compra externa",
                "estado": f"Compra a {c.get('vendedor_nombre') or 'tercero'}",
                "contrato": c.get("nombre"),
                "contrato_id": c.get("id"),
                "codigo_sic": None,
                "pct": None,
                "desde": p.get("segmento_inicio") or c.get("fecha_inicio"),
                "hasta": p.get("segmento_fin") or c.get("fecha_fin"),
                "dias": None,
                "mwh_real": None, "mwh_proyectado": None, "mwh_total": None,
            }
            inventario.append(filas_g)

    # Plantas externas que ADEMÁS están cayendo en el residuo de bolsa: su energía
    # ya está comprada por PPA, así que contarlas como venta en bolsa UNGG infla
    # justo la línea más sensible. No se excluyen en silencio — se declaran, y
    # `excluir_compra_externa` deja ver el otro número.
    compra_externa_en_bolsa = [
        {"proyecto_id": f["proyecto_id"], "planta": f["planta"],
         "frontera": f["frontera"], "categoria": f["categoria"],
         "mwh_total": f["mwh_total"]}
        for f in inventario
        if f["categoria"] in ("e", "f") and f["proyecto_id"] in ids_compra_externa
    ]

    salida = {
        "periodo": periodo,
        "balance": balance,
        "inventario": inventario,
        "excluir_compra_externa": excluir_compra_externa,
        "advertencias": {
            "sin_datos": sin_datos,
            "pct_anomalos": derivado["anomalias"],
            "compra_externa_en_bolsa": compra_externa_en_bolsa,
            "tramos_estimados": tramos_estimados,
        },
    }
    if warning:
        salida["warning"] = warning
    return salida


# Ventana (días) hacia atrás para estimar la tasa diaria de generación del mes futuro.
_DIAS_TASA_REF = 30


def _plantas_contratos_de(year: int, month: int, incluir_todos: bool = False) -> dict:
    """Payload de plantas-contratos del mes (aislado para poder mockear en tests)."""
    from apps.mercado_xm.services.cumplimiento.piscinas import plantas_contratos
    return plantas_contratos(year, month, incluir_todos=incluir_todos)


def _tasa_diaria_reciente(plantas: dict, hoy: date) -> dict:
    """{proyecto_id: MWh/día} desde la generación de los últimos _DIAS_TASA_REF días.
    Aislado para mockear en tests (hace red)."""
    from apps.energia.services.comercializacion import identificador_monitoreo as _mon_id
    from apps.mercado_xm.services.cumplimiento.xm_api import _fetch_range, _unergy_token
    from apps.proyectos.models import Proyecto
    sub = {}
    if plantas:
        for p in Proyecto.objects.filter(id__in=list(plantas)):
            sp = _mon_id(p)
            if sp:
                sub[p.id] = sp
    if not sub:
        return {}
    try:
        token = _unergy_token()
    except Exception:
        logger.error("Auth Unergy failed in balance proyectado")
        return {}
    desde = hoy - timedelta(days=_DIAS_TASA_REF)
    hasta = hoy - timedelta(days=1)
    sps = sorted(set(sub.values()))
    por_sp: dict[str, float | None] = {}
    with ThreadPoolExecutor(max_workers=min(len(sps), 12)) as pool:
        for sp, res in pool.map(lambda s: (s, _fetch_range(token, s, desde, hasta)), sps):
            por_sp[sp] = res.get("mwh")
    dias = max(1, (hasta - desde).days + 1)
    return {pid: (por_sp.get(sp) / dias) for pid, sp in sub.items() if por_sp.get(sp) is not None}


def calcular_balance_proyectado(year: int, month: int, hoy: date | None = None,
                                incluir_todos: bool = False) -> dict:
    """Balance de bolsa de un mes FUTURO: contratos de ese mes × tasa de generación
    reciente, todo proyectado. Misma forma de salida que `calcular_balance` (balance/periodo)."""
    hoy = hoy or hoy_col()
    total_dias = calendar.monthrange(year, month)[1]
    first_day = date(year, month, 1)
    last_day = date(year, month, total_dias)

    data = _plantas_contratos_de(year, month, incluir_todos=incluir_todos)
    derivado = construir_tramos(data, first_day, last_day)
    plantas = derivado["plantas"]
    tasa = _tasa_diaria_reciente(plantas, hoy)
    energia = _energia_proyectada(plantas, tasa, first_day, last_day)
    balance = agregar_balance(plantas, energia)
    return {
        "periodo": {"year": year, "month": month, "dias_mes": total_dias,
                    "es_proyeccion": True, "dias_tasa_ref": _DIAS_TASA_REF},
        "balance": balance,
    }
