"""A quién le llega el reporte CGM, y con qué adjuntos.

Puerto de `app/api/v1/reporte_cgm.py`. Acá vive todo lo que toca la base
—resolver destinatarios, sus correos y sus fronteras— más la orquestación del
lote de llamadas a Quoia. El cálculo y el Excel están en
`apps.energia.services.cgm`, que no sabe de la base ni de destinatarios.

El cliente SMTP sigue en `app/services/email_service.py`: no toca la base y se
portará cuando se retire FastAPI, como los clientes de MGS.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.db import close_old_connections

from apps.clientes.models import Cliente
from apps.clientes.services import contactos
from apps.energia.services import cgm as svc
from apps.energia.services.reporte import borders as borders_svc
from apps.energia.services.reporte import curvas as curvas_energia
from apps.fronteras.models import Frontera, OperadorRed
from apps.proyectos.models import ProyectoInfoTecnica

logger = logging.getLogger("operaciones.reporte_cgm")

TIPOS_CONSUMO = {"consumo", "consumo_auxiliar", "consumo_propio"}

# cliente_id -- pedido puntual: un Excel de Cliente (3 hojas) POR PROYECTO en
# vez de uno combinado, todos adjuntos al mismo correo. 75 = CGM Ingeniería
# (proyectos: GD San Pelayo, GD La Hormiguita). No requiere cambios en el
# front -- se decide acá por el id del cliente.
CLIENTES_EXCEL_POR_PROYECTO: set[int] = {75}

# cliente_id -- pedido puntual: en vez de resolver sus fronteras por
# proyectos vinculados (ProyectoAreaContacto/ProyectoInversionista), recibe
# TODAS las fronteras del sistema en formato Cliente. 157 = "Operaciones
# Unergy" (creado para esto -- correo operaciones@unergy.io, sin vincular
# ningún proyecto a propósito, para no tener que mantener esa lista al día
# cada vez que se crea un proyecto nuevo).
CLIENTES_TODAS_LAS_FRONTERAS: set[int] = {157}


def _fronteras_de_operador(operador_id: int) -> list[Frontera]:
    return list(
        Frontera.objects.filter(
            operador_red_id=operador_id, deleted_at__isnull=True,
        ).select_related("proyecto")
    )


def _fronteras_de_cliente(cliente_id: int) -> list[Frontera]:
    """Fronteras de los proyectos donde este cliente es la fuente del contacto
    CGM (por puntero de área, o por ser inversionista vigente) -- no depende
    de quién sea el titular del proyecto. Excepción: CLIENTES_TODAS_LAS_FRONTERAS
    se salta ese filtro por completo y trae todo."""
    base = Frontera.objects.filter(deleted_at__isnull=True).select_related("proyecto")
    if cliente_id in CLIENTES_TODAS_LAS_FRONTERAS:
        return list(base)
    proyecto_ids = contactos.proyecto_ids_por_cliente("cgm", cliente_id)
    if not proyecto_ids:
        return []
    return list(base.filter(proyecto_id__in=proyecto_ids))


def _datos_proyectos_para_resumen(gaia, fronteras: list[Frontera]) -> dict[int, dict]:
    """`{proyecto_id: {...}}` para `svc.calcular_resumen_mensual` (Hoja 2 de
    Clientes) -- agrupa las fronteras de ESTE destinatario por proyecto,
    separando cuál es la de Generación (para medidor/capacidad_efectiva) y
    cuál la de Consumo."""
    proyecto_ids = {f.proyecto_id for f in fronteras if f.proyecto_id}
    if not proyecto_ids:
        return {}

    # Numeric() en Postgres llega como Decimal, no float -- sin este cast,
    # "total_gen / capacidad_dc" en calcular_resumen_diario/mensual explota
    # con "unsupported operand type(s) for /: 'float' and 'decimal.Decimal'"
    # (bug real 2026-08-11, recién visible porque el backfill de Solenium
    # llenó capacidad_instalada_kwp en proyectos que antes tenían None).
    capacidad_dc: dict[int, float | None] = {
        pid: float(cap) if cap is not None else None
        for pid, cap in ProyectoInfoTecnica.objects.filter(
            proyecto_id__in=proyecto_ids
        ).values_list("proyecto_id", "capacidad_instalada_kwp")
    }

    mapa_borders = curvas_energia.construir_mapa_borders(gaia)

    proyectos: dict[int, dict] = {}
    for f in fronteras:
        if not f.proyecto_id or not f.proyecto:
            continue
        datos = proyectos.setdefault(f.proyecto_id, {
            "nombre": f.proyecto.nombre_comercial,
            "frt_gen": None, "frt_con": None,
            "capacidad_dc_kwp": capacidad_dc.get(f.proyecto_id),
            "capacidad_efectiva_mw": None,
            "main_meter_gen": None, "backup_meter_gen": None,
            "main_meter_con": None, "backup_meter_con": None,
        })
        meta = (
            mapa_borders.get(f.codigo_frontera.strip().lower())
            if f.codigo_frontera else None
        )
        if f.tipo_frontera == "generacion" and f.codigo_frontera:
            datos["frt_gen"] = f.codigo_frontera
            if f.proyecto.potencia_ac_kw is not None:
                datos["capacidad_efectiva_mw"] = (
                    float(f.proyecto.potencia_ac_kw) / 1000
                )
            if meta:
                datos["main_meter_gen"] = meta.get("main_meter")
                datos["backup_meter_gen"] = meta.get("backup_meter")
        elif f.tipo_frontera in TIPOS_CONSUMO and f.codigo_frontera:
            # consumo_auxiliar/consumo_propio son el autoconsumo de la misma
            # planta de generación (ej. Sol&Cielo 7 Los Bongos) -- cuentan
            # igual como "Total Consumo" para este resumen, no solo 'consumo'.
            datos["frt_con"] = f.codigo_frontera
            if meta:
                datos["main_meter_con"] = meta.get("main_meter")
                datos["backup_meter_con"] = meta.get("backup_meter")
        elif f.codigo_frontera:
            # tipo_frontera == generacion_consumo (o cualquier tipo futuro)
            # no encaja en ninguna de las dos ramas -- ni "generación" ni
            # TIPOS_CONSUMO. Hoy (2026-08-26) 0 fronteras activas tienen
            # este tipo, así que nunca se disparó en producción, pero antes
            # quedaba silenciosamente sin frt_gen/frt_con/medidores en el
            # resumen (el proyecto aparecía con todo en None, sin ninguna
            # señal de que faltó clasificar) -- auditoría CGM 2026-08-26,
            # finding #9. Se loguea en vez de asumir un tratamiento (gen,
            # consumo, o ambos) que nadie validó todavía con un caso real.
            logger.warning(
                "Frontera %s (proyecto %s) con tipo_frontera=%s sin clasificar "
                "en el resumen de Cliente -- no encaja en generación ni en "
                "consumo, revisar manualmente.",
                f.codigo_frontera, f.proyecto_id, f.tipo_frontera,
            )
    return proyectos


def _excels_cliente_por_proyecto(
    gaia, fronteras: list[Frontera], filas_por_frt: dict[str, list[dict]],
    dias: list[str], dias_mes: list[str], es_ultimo_dia_mes: bool,
    fecha_inicio, fecha_archivo: str,
) -> list[tuple[bytes, str]]:
    """Igual que la rama normal de Cliente (3 hojas: Reporte Acumulado +
    Resumen Diario + Resumen Mensual si aplica), pero un Excel POR proyecto
    en vez de uno combinado -- agrupa `fronteras` (ya filtradas a este
    destinatario) por proyecto_id. Ver CLIENTES_EXCEL_POR_PROYECTO."""
    por_proyecto: dict[int, list[Frontera]] = {}
    for f in fronteras:
        # Mismo guard que _datos_proyectos_para_resumen: sin esto, un
        # proyecto_id huérfano (FK sin fila viva del otro lado) revienta con
        # AttributeError en `.proyecto.nombre_comercial` más abajo y tumba el
        # envío completo de CGM para este cliente, no solo esta fila.
        if f.proyecto_id and f.proyecto:
            por_proyecto.setdefault(f.proyecto_id, []).append(f)

    # Una sola resolución para TODOS los proyectos de este cliente -- antes
    # se llamaba _datos_proyectos_para_resumen() (1 query a ProyectoInfoTecnica
    # cada vez) UNA VEZ POR PROYECTO dentro del loop de abajo, un N+1 real
    # aunque de bajo impacto hoy (CLIENTES_EXCEL_POR_PROYECTO son 2 proyectos
    # -- auditoría CGM 2026-08-26, finding #8).
    proyectos_todos = _datos_proyectos_para_resumen(gaia, fronteras)

    adjuntos: list[tuple[bytes, str]] = []
    for proyecto_id, fronteras_proyecto in por_proyecto.items():
        proyectos = (
            {proyecto_id: proyectos_todos[proyecto_id]}
            if proyecto_id in proyectos_todos else {}
        )
        filas_todas_proyecto = [
            fila for f in fronteras_proyecto if f.codigo_frontera
            for fila in filas_por_frt.get(f.codigo_frontera, [])
        ]
        filas_resumen_diario = svc.calcular_resumen_diario(
            gaia, proyectos, filas_por_frt, dias[0])
        filas_resumen_mensual = None
        if es_ultimo_dia_mes:
            mes_titulo = (
                f"{svc.nombre_mes(fecha_inicio).capitalize()} {fecha_inicio.year}")
            filas_resumen_mensual = svc.calcular_resumen_mensual(
                gaia, proyectos, filas_por_frt, dias_mes, mes_titulo)
        excel_bytes = svc.generar_excel_cliente(
            filas_todas_proyecto, filas_resumen_diario, filas_resumen_mensual)

        nombre_proyecto = fronteras_proyecto[0].proyecto.nombre_comercial
        slug_proyecto = "".join(
            c if c.isalnum() else "_" for c in nombre_proyecto.lower()).strip("_")
        adjuntos.append((excel_bytes, f"cgm-report-{fecha_archivo}-{slug_proyecto}.xlsx"))
    return adjuntos


def _nombres_proyectos(fronteras: list[Frontera]) -> list[str]:
    """Nombres únicos de proyecto entre estas fronteras (una misma planta
    suele tener frontera de Generación y de Consumo por separado)."""
    vistos: dict[int, str] = {}
    for f in fronteras:
        if f.proyecto and f.proyecto_id not in vistos:
            vistos[f.proyecto_id] = f.proyecto.nombre_comercial
    return sorted(vistos.values())


def _resultado(dest, nombre, correos, fronteras, ok, **extra) -> dict:
    return {
        "tipo": dest["tipo"], "id": dest["id"], "nombre": nombre,
        "correos": correos, "fronteras": fronteras, "ok": ok,
        "error": extra.get("error"), "warning": extra.get("warning"),
    }


def enviar(fecha_inicio, fecha_fin, destinatarios: list[dict]) -> dict:
    """Genera y manda el reporte CGM a cada destinatario."""
    from app.services.mgs.gaia_client import GaiaClient

    if fecha_fin < fecha_inicio:
        fecha_inicio, fecha_fin = fecha_fin, fecha_inicio

    dias = [
        (fecha_inicio + timedelta(days=i)).isoformat()
        for i in range((fecha_fin - fecha_inicio).days + 1)
    ]
    fecha_display = dias[0] if len(dias) == 1 else f"{dias[0]} a {dias[-1]}"
    fecha_archivo = dias[0] if len(dias) == 1 else f"{dias[0]}_a_{dias[-1]}"

    # Envío de un solo día -- dispara cosas distintas, cada una acotada a su
    # tipo de destinatario:
    #  - Operador de Red: SOLO si además ese día es el último del mes, se
    #    adjunta ADEMÁS (no en vez de) un segundo Excel con todo el mes.
    #  - Cliente: el reporte diario mismo pasa a tener tres hojas -- 'Diario
    #    acumulado' (desde el día 1 del mes hasta hoy) y 'Resumen Diario'
    #    (mismas variables del resumen, solo de hoy) TODOS los días; 'Resumen
    #    Mensual' (las mismas variables pero acumuladas del mes completo)
    #    únicamente el último día del mes.
    # dias_mes ya incluye dias[0], así que en ambos casos se pide a Quoia una
    # sola vez (superset) en vez de dos.
    es_dia_unico = len(dias) == 1
    dias_mes = svc.dias_del_mes(fecha_inicio) if es_dia_unico else []
    es_ultimo_dia_mes = es_dia_unico and svc.es_ultimo_dia_del_mes(fecha_inicio)
    dias_fetch = dias_mes if es_dia_unico else dias

    # 1. Resolver, desde la BD, a quién le llega qué (nunca se confía en datos
    #    del frontend más allá de tipo+id).
    items: list[dict] = []
    for dest in destinatarios:
        if dest["tipo"] == "operador":
            operador = OperadorRed.objects.filter(
                id=dest["id"]).prefetch_related("contactos").first()
            if not operador:
                items.append({"dest": dest, "nombre": f"Operador #{dest['id']}",
                              "correos": [], "fronteras": []})
                continue
            fronteras = _fronteras_de_operador(dest["id"])
            correos = [c.email for c in operador.contactos.all()]
            nombre = operador.nombre_comercial or operador.nombre_legal
        else:
            cliente = Cliente.objects.filter(id=dest["id"]).first()
            if not cliente:
                items.append({"dest": dest, "nombre": f"Cliente #{dest['id']}",
                              "correos": [], "fronteras": []})
                continue
            fronteras = _fronteras_de_cliente(dest["id"])
            correos = contactos.correos("cgm", cliente_id=dest["id"])
            nombre = cliente.razon_social_nombre

        proyectos_total = len(_nombres_proyectos(fronteras))

        # Filtro opcional a proyectos puntuales dentro de este destinatario --
        # el frontend manda None cuando el usuario no seleccionó ningún
        # proyecto puntual dentro del destinatario (equivale a "sin filtro",
        # todas sus fronteras -- ver ReporteCGMView.vue, enviarSeleccionados())
        # y una lista no vacía cuando sí hay una selección explícita.
        if dest.get("proyectos") is not None:
            proyectos_ids = set(dest["proyectos"])
            fronteras = [f for f in fronteras if f.proyecto_id in proyectos_ids]

        items.append({
            "dest": dest, "nombre": nombre, "correos": correos,
            "fronteras": fronteras, "proyectos_total": proyectos_total,
        })

    # 2. Un solo lote de llamadas a Quoia -- solo los frt_codes que realmente
    #    hacen falta, y solo por los días que cada destinatario en verdad
    #    necesita. Antes dias_fetch (el mes completo cuando es_dia_unico) se
    #    aplicaba a TODOS los frt_codes de la request por igual, aunque un
    #    Operador de Red normal (no fin de mes) solo usa filas_dia -- pedir
    #    el mes entero para él era puro desperdicio (auditoría CGM
    #    2026-08-26, finding #3). Cliente sí necesita mes-a-la-fecha siempre
    #    (hoja "Diario acumulado"); Operador solo cuando es_ultimo_dia_mes.
    dias_por_frt: dict[str, list[str]] = {}
    for item in items:
        necesita_mes = (
            item["dest"]["tipo"] == "cliente" or es_ultimo_dia_mes or not es_dia_unico
        )
        dias_item = dias_fetch if necesita_mes else dias
        for f in item["fronteras"]:
            if not f.codigo_frontera:
                continue
            actual = dias_por_frt.get(f.codigo_frontera)
            # Si otro destinatario que comparte esta misma frontera ya pidió
            # el superset (mes completo), no lo reducimos a un solo día.
            if actual is None or len(dias_item) > len(actual):
                dias_por_frt[f.codigo_frontera] = dias_item
    frt_codes = set(dias_por_frt)

    gaia = GaiaClient()
    filas_por_frt: dict[str, list[dict]] = {}
    if frt_codes:
        borders = borders_svc.resolver_borders(gaia, frt_codes)
        # UNA llamada por frontera (fetch_filas_rango, paginada) cubriendo
        # TODOS sus días de una vez, no una por (frontera, día) -- antes
        # "Operaciones Unergy" (~300 fronteras) x mes-a-la-fecha podía ser
        # miles de llamadas HTTP que en la práctica repaginaban básicamente
        # lo mismo (auditoría CGM 2026-08-26, finding #4). El paralelismo se
        # mantiene por las mismas dudas de escala: el caso real de
        # "Operaciones Unergy" excedía el timeout del frontend (30s).
        # Ninguna tarea toca la base, así que el pool no necesita cerrar
        # conexiones -- solo habla con Quoia.
        frt_validos = [fc for fc in frt_codes if borders.get(fc.lower()) is not None]
        # No aparece en el listado de Quoia (caso real 2026-07-10:
        # Bayunca/San Onofre registrados ahí bajo otra compañía) -- no hay
        # nombre ni dato real que reportar, así que no se incluye ninguna
        # fila para ese frt_code. Distinto del caso "sí está en Quoia pero no
        # reportó esa fecha" (eso sí se deja como "Sin reporte" dentro de
        # fetch_filas_rango).
        if frt_validos:
            with ThreadPoolExecutor(max_workers=12) as pool:
                resultados_filas = pool.map(
                    lambda fc: (fc, svc.fetch_filas_rango(
                        gaia, fc, borders.get(fc.lower()), dias_por_frt[fc])),
                    frt_validos,
                )
                for frt_code, filas in resultados_filas:
                    filas_por_frt[frt_code] = filas
            close_old_connections()

    # 3. Generar y enviar un Excel por destinatario, filtrado a sus fronteras.
    from app.services import email_service

    resultados = []
    for item in items:
        dest, nombre = item["dest"], item["nombre"]
        correos, fronteras = item["correos"], item["fronteras"]
        proyectos_total = item.get("proyectos_total", 0)

        if not fronteras:
            resultados.append(_resultado(
                dest, nombre, correos, 0, False,
                error="No hay fronteras vinculadas"))
            continue
        if not correos:
            resultados.append(_resultado(
                dest, nombre, [], len(fronteras), False,
                error="Sin correos configurados"))
            continue

        filas_todas = [
            fila
            for f in fronteras if f.codigo_frontera
            for fila in filas_por_frt.get(f.codigo_frontera, [])
        ]
        # filas_todas trae todo el mes cuando es_dia_unico (dias_fetch =
        # dias_mes) -- filas_dia se queda solo con el día pedido, que es lo
        # que usa Operador de Red siempre y Cliente solo si pidió un rango
        # explícito (no un solo día, ver más abajo).
        dias_set = set(dias)
        filas_dia = (
            [f for f in filas_todas if f["report date"] in dias_set]
            if es_dia_unico else filas_todas
        )

        try:
            slug = "".join(
                c if c.isalnum() else "_" for c in nombre.lower()).strip("_")
            excel_mensual_bytes = filename_mensual = mes_str = None
            adjuntos_extra: list[tuple[bytes, str]] = []
            filename_principal = f"cgm-report-{fecha_archivo}-{slug}.xlsx"
            fecha_str_envio = fecha_display

            es_cliente = dest["tipo"] == "cliente"
            if es_cliente and es_dia_unico and dest["id"] in CLIENTES_EXCEL_POR_PROYECTO:
                # Pedido puntual (ver CLIENTES_EXCEL_POR_PROYECTO): un Excel
                # por proyecto en vez de uno combinado, todos en el mismo
                # correo -- el primero se manda como adjunto principal, el
                # resto como adjuntos_extra.
                adjuntos = _excels_cliente_por_proyecto(
                    gaia, fronteras, filas_por_frt, dias, dias_mes,
                    es_ultimo_dia_mes, fecha_inicio, fecha_archivo,
                )
                excel_bytes, filename_principal = adjuntos[0]
                adjuntos_extra = adjuntos[1:]
                fecha_str_envio = f"{dias_mes[0]} a {dias_mes[-1]}"
            elif es_cliente and es_dia_unico:
                # Cliente: 'Reporte Acumulado' (mes completo hasta hoy) +
                # 'Resumen Diario' (mismas variables, solo hoy) siempre;
                # 'Resumen Mensual' (acumulado del mes) solo el último día.
                proyectos = _datos_proyectos_para_resumen(gaia, fronteras)
                filas_resumen_diario = svc.calcular_resumen_diario(
                    gaia, proyectos, filas_por_frt, dias[0])
                filas_resumen_mensual = None
                if es_ultimo_dia_mes:
                    mes_titulo = (
                        f"{svc.nombre_mes(fecha_inicio).capitalize()} "
                        f"{fecha_inicio.year}"
                    )
                    filas_resumen_mensual = svc.calcular_resumen_mensual(
                        gaia, proyectos, filas_por_frt, dias_mes, mes_titulo)
                excel_bytes = svc.generar_excel_cliente(
                    filas_todas, filas_resumen_diario, filas_resumen_mensual)
                fecha_str_envio = f"{dias_mes[0]} a {dias_mes[-1]}"
            else:
                excel_bytes = svc.generar_excel(filas_dia)
                if es_ultimo_dia_mes and dest["tipo"] == "operador":
                    excel_mensual_bytes = svc.generar_excel(
                        filas_todas,
                        titulo_hoja=svc.titulo_hoja_mensual(fecha_inicio),
                    )
                    filename_mensual = (
                        f"cgm-report-consolidado-"
                        f"{fecha_inicio.strftime('%Y-%m')}-{slug}.xlsx"
                    )
                    mes_str = svc.nombre_mes(fecha_inicio)

            email_service.send_reporte_cgm_email(
                to_emails=correos,
                excel_bytes=excel_bytes,
                filename=filename_principal,
                fecha_str=fecha_str_envio,
                destinatario_nombre=nombre,
                proyectos=_nombres_proyectos(fronteras),
                proyectos_total=proyectos_total,
                excel_mensual_bytes=excel_mensual_bytes,
                filename_mensual=filename_mensual,
                mes_str=mes_str,
                adjuntos_extra=adjuntos_extra,
                cliente_id=dest["id"] if es_cliente else None,
                operador_red_id=dest["id"] if not es_cliente else None,
            )
            frts_con_error_conexion = {
                fila["border frtcode"] for fila in filas_todas
                if fila.get("state") == "Error de conexión con Quoia"
            }
            warning = (
                f"{len(frts_con_error_conexion)} frontera(s) con error de conexión a "
                "Quoia -- dato incompleto para esa(s) fecha(s), revisar manualmente"
                if frts_con_error_conexion else None
            )
            resultados.append(_resultado(
                dest, nombre, correos, len(fronteras), True, warning=warning))
        except Exception as exc:
            resultados.append(_resultado(
                dest, nombre, correos, len(fronteras), False, error=str(exc)))

    return {"resultados": resultados}
