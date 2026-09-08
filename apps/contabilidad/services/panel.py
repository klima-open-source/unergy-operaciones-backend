"""Guardar un panel contable: sus líneas, su reparto y sus costos de módulo.

Puerto de `_guardar_panel` y `clasificacion_vigente` de
`app/api/v1/panel_contable.py`.

**Los costos de módulo mandan sobre el ER, pero un fallo suyo no tumba la carga.**
Si el proyecto tiene contrato de mantenimiento o arriendo, el valor del módulo
reemplaza al del Excel; si el cálculo revienta, se deja el del ER y se sigue.

**La clasificación NEU/Nitro se HEREDA del período anterior.** Se guarda por
período, pero son plantas muy estables: exigir que alguien la recargue cada mes
convierte un olvido en una liquidación mal armada — pasó en 2026-07, donde el
último registro era de junio y los cuatro NEU habrían pasado por el camino de la
API. Se toma el registro más reciente ANTERIOR o igual al período, así que
reclasificar un mes concreto sigue mandando, y nunca se hereda del futuro.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

from django.db import transaction
from django.db.models import Exists, OuterRef, Q

from rest_framework.exceptions import NotFound

from api.exceptions import Conflict, NoProcesable, ServicioNoDisponible
from apps.contabilidad.models import (
    AliasFuenteIngreso, ClasificacionLiquidacion, MapeoCeldaConcepto,
    PanelConsecutivo, PanelContable, PanelContableLinea, PanelSoporte,
)
from apps.contabilidad.services.costos import (
    aplicar_costos_modulo, valores_facturas_modulo, valores_modulo_costos,
)
from apps.contabilidad.services.er_loader import (
    IVA, _norm as _norm_concepto, extraer_proyecto_de_archivo, parsear_er,
    recalcular_er,
)
from apps.contabilidad.services.desde_api import construir_parsed
from apps.contabilidad.services.reparto import (
    _construir_lineas_base, _inversionistas_de, _inversionistas_de_batch,
)
from apps.clientes.models import Cliente
from apps.contabilidad.services.impuestos import (
    impuestos_de_factura, overrides_tasa_servicio, tasas_efectivas,
)
from apps.liquidaciones.services import api_externa
from apps.proyectos.models import Proyecto, ProyectoInversionista

logger = logging.getLogger("operaciones.panel_contable")


def guardar_panel(proyecto_id: int, periodo: str, tipo: str, parsed: dict,
                  er_filename: str | None, usuario_id: int,
                  origen: str = "er") -> PanelContable:
    """Crea o REEMPLAZA el panel de un proyecto+período, con sus líneas ya
    divididas por inversionista.

    Recargar borra las líneas previas con un solo DELETE y no fila por fila: con
    decenas de paneles el borrado por ORM se vuelve tan lento que el proxy corta
    la petición (504 → "Fallo al procesar ER"). Los flags y consecutivos del
    panel se preservan.
    """
    panel = PanelContable.objects.filter(
        proyecto_id=proyecto_id, periodo=periodo, tipo=tipo,
    ).first()
    if panel is None:
        panel = PanelContable.objects.create(
            proyecto_id=proyecto_id, periodo=periodo, tipo=tipo,
        )
    else:
        PanelContableLinea.objects.filter(panel_id=panel.id).delete()

    base = _construir_lineas_base(parsed)
    # Costos que el Panel toma de los MÓDULOS (piloto: Mantenimiento y Arrendamiento).
    # Si el proyecto tiene contrato de ese servicio, el valor del módulo MANDA sobre
    # el del ER; si no, se conserva el del ER. Ver app/services/costos_panel.py.
    try:
        mods = valores_modulo_costos(proyecto_id, periodo)
        # Representación/CGM = tarifa app × energía; Administración = tarifa_admin ×
        # ingreso. Las dos bases son la VENTA de energía, no el neto de compras:
        # cuando el traductor de la API las entrega (`base_tarifa_*`) mandan ellas.
        # NEU y Nitro no pasan por aquí: su Excel tiene su propia fórmula, donde la
        # compra sí resta, y ese camino se deja intacto con kwh/total_ingresos.
        base_kwh = parsed.get("base_tarifa_kwh")
        base_cop = parsed.get("base_tarifa_cop")
        mods.update(valores_facturas_modulo(
            proyecto_id, periodo,
            parsed.get("kwh") if base_kwh is None else base_kwh,
            parsed.get("total_ingresos") if base_cop is None else base_cop))
        if mods:
            base = aplicar_costos_modulo(base, mods, iva=IVA)
    except Exception:
        # Un problema calculando el módulo no debe tumbar la carga del ER: se deja
        # el costo del ER y se sigue.
        logger.exception("No se pudieron aplicar los costos de módulo (proy=%s, per=%s)", proyecto_id, periodo)
    tiene_costos = any(l["grupo"] == "costos" for l in base)

    panel.ingreso_bruto_cop = parsed["ingreso_bruto"]
    panel.comercializador = parsed.get("comercializador")
    panel.tiene_bolsa = bool(parsed.get("tiene_bolsa"))
    panel.tiene_costos = tiene_costos
    # Sin costos → no se puede liquidar costos (checkbox deshabilitado en la vista).
    if not tiene_costos:
        panel.liquidar_costos = False
    panel.er_filename = er_filename
    panel.origen = origen
    # Snapshot del ER recalculado: permite releer una celda al cambiar el mapeo
    # sin re-subir el archivo. Se guarda como JSON {hoja: {coord: valor}}.
    snap = parsed.get("snapshot") or {}
    panel.er_snapshot = json.dumps(snap) if snap else None
    panel.generado_por_id = usuario_id
    panel.save()

    invs = _inversionistas_de(proyecto_id, periodo)
    if not invs:
        invs = [{"id": None, "nombre": "Sin inversionistas", "fraccion": 1.0, "pct": 100.0}]

    # Un reparto que no suma 100% se lleva plata a ninguna parte o la duplica, y
    # sin este aviso se dividiría en silencio. Puede pasar si un inversionista
    # termina a mitad de mes: el saliente y el entrante se traslapan y suman 200%.
    suma_pct = sum(i["pct"] or 0 for i in invs)
    if abs(suma_pct - 100) >= 0.6:
        logger.warning(
            "Reparto de %s en %s suma %.2f%% entre %d inversionistas, no 100%%",
            proyecto_id, periodo, suma_pct, len(invs),
        )

    orden = 0
    filas = []
    for inv in invs:
        frac = inv["fraccion"] if inv["fraccion"] is not None else 0.0
        for l in base:
            filas.append(PanelContableLinea(
                panel_id=panel.id,
                proyecto_inversionista_id=inv["id"],
                inversionista_nombre=inv["nombre"],
                porcentaje=inv["pct"],
                grupo=l["grupo"],
                concepto=l["concepto"],
                valor_cop=round(l["valor"] * frac, 2),
                hoja=l.get("hoja"),
                celda=l.get("celda"),
                fuente=l.get("fuente"),
                orden=orden,
            ))
            orden += 1
    PanelContableLinea.objects.bulk_create(filas)
    return panel


def clasificacion_vigente(periodo: str) -> dict[int, str]:
    """`{proyecto_id: tipo}` para el período, heredando del anterior.

    La clasificación NEU/Nitro se guarda por período, pero son plantas muy
    estables: lo normal es que un proyecto siga siendo NEU mes tras mes. Exigir
    que alguien la recargue cada mes convierte un olvido en una liquidación mal
    armada -- pasó en 2026-07, donde el último registro era de junio y los cuatro
    NEU habrían pasado por el camino de la API.

    Se toma el registro más reciente ANTERIOR o igual al período, así que
    reclasificar un mes concreto sigue mandando y no lo pisa la herencia. Y no se
    hereda del futuro: clasificar agosto no cambia cómo se armó julio.
    """
    filas = ClasificacionLiquidacion.objects.filter(periodo__lte=periodo)
    vigente: dict[int, tuple[str, str]] = {}
    for c in filas:
        anterior = vigente.get(c.proyecto_id)
        if anterior is None or c.periodo > anterior[0]:
            vigente[c.proyecto_id] = (c.periodo, c.tipo)
    return {pid: tipo for pid, (_, tipo) in vigente.items()}


def _normalizar_periodo(periodo: str) -> tuple[str, int, int]:
    """`2026-7` y `2026-07` son el mismo mes. Devuelve (YYYY-MM, año, mes)."""
    try:
        y, m = periodo.strip().split("-")
        y, m = int(y), int(m)
        if not 1 <= m <= 12:
            raise ValueError
    except Exception:
        raise NoProcesable("El período debe tener formato YYYY-MM")
    return f"{y:04d}-{m:02d}", y, m


def minigranjas_operativas():
    """El Panel Contable es SOLO de minigranjas operativas.

    Deja fuera AMC, Acanto, los COLxxx y todo lo que esté en desarrollo.
    """
    from apps.proyectos.models import Proyecto, ProyectoInversionista

    return Proyecto.objects.filter(tipo_proyecto="minigranja", estado="en_operacion")


def representamos():
    """`Q` de "representamos el proyecto".

    Criterio SEGURO, el mismo que liquidaciones: flag `srv_representacion` activo
    O contrato de representación vigente. El flag y el contrato a veces se
    contradicen; se conserva si CUALQUIERA indica representación, para no dejar
    de liquidar algo que sí representamos.
    """
    from apps.contratos.models import ContratoServicio

    con_contrato = ContratoServicio.objects.filter(
        proyecto_id=OuterRef("pk"), servicio_aplica="representacion", estado="vigente",
    )
    return Q(srv_representacion=True) | Q(Exists(con_contrato))


def cargar_er(archivos: list, periodo: str, tipo: str, tipo_carga: str,
              usuario_id: int) -> dict:
    """Sube uno o varios ER. Por cada archivo: recalcula con LibreOffice, parsea,
    matchea el proyecto, divide por % del backend y guarda el panel + líneas.
    periodo: YYYY-MM. tipo: 'preliquidacion' | 'oficial'.
    tipo_carga: 'normal' | 'neu' | 'nitro' — cómo se lee la sección de ingresos.

    VALIDACIÓN CRUZADA: cada ER debe cargarse en su sección. Si el proyecto está
    clasificado distinto a `tipo_carga` para el período, se rechaza (no se guarda)
    y se reporta aparte, sin romper la carga de los válidos.
    """
    tipo = (tipo or "preliquidacion").strip().lower()
    if tipo not in ("preliquidacion", "oficial"):
        raise NoProcesable("tipo debe ser 'preliquidacion' u 'oficial'")
    tipo_carga = (tipo_carga or "normal").strip().lower()
    if tipo_carga not in ("normal", "neu", "nitro"):
        raise NoProcesable("tipo_carga debe ser 'normal', 'neu' o 'nitro'")
    periodo_norm, _anio, _mes = _normalizar_periodo(periodo)

    # Solo minigranjas operativas: un ER de otro tipo de proyecto no debe crear panel.
    proyectos_db = [
        {"id": p.id, "nombre_comercial": p.nombre_comercial}
        for p in minigranjas_operativas().order_by("id")
    ]

    # Clasificación del período: proyecto_id → tipo (default 'normal').
    # Hereda del período anterior, igual que el resto: si una planta NEU no se
    # reclasificó este mes, su Excel tiene que seguir entrando.
    clasif_map = clasificacion_vigente(periodo_norm)

    # Mapeos guardados: proyecto_id → {concepto_norm: {hoja, celda}}. Si existe un
    # mapeo confirmado para (proyecto, concepto), el parser lee ESA celda.
    mapeos_por_proyecto: dict[int, dict] = {}
    for m in MapeoCeldaConcepto.objects.all():
        mapeos_por_proyecto.setdefault(m.proyecto_id, {})[_norm_concepto(m.concepto)] = {
            "hoja": m.hoja, "celda": m.celda,
        }

    # Alias de fuentes de ingreso: proyecto_id → {columna_origen.lower(): {etiqueta, orden}}.
    aliases_por_proyecto: dict[int, dict] = {}
    for a in AliasFuenteIngreso.objects.all():
        aliases_por_proyecto.setdefault(a.proyecto_id, {})[a.columna_origen.lower()] = {
            "etiqueta": a.etiqueta, "orden": a.orden,
        }

    resultados = {
        "cargados": [], "sin_match": [], "errores": [],
        "warnings": [], "duplicados": [], "rechazados": [],
    }
    proyectos_vistos: set[int] = set()

    for uf in archivos:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
        recalc_path = None
        try:
            tmp.write(uf.read())
            tmp.flush()
            tmp.close()

            # El proyecto va al final del nombre de archivo; ventanas deslizantes.
            proy = extraer_proyecto_de_archivo(uf.name or "", proyectos_db)
            if not proy:
                resultados["sin_match"].append(uf.name)
                continue

            # Multi-ER: cada ER es el 100% del proyecto. Si ya cargamos uno de
            # este proyecto en esta llamada, ignoramos los demás (El Son, Baraya…).
            if proy["id"] in proyectos_vistos:
                resultados["duplicados"].append({
                    "archivo": uf.name, "proyecto": proy["nombre_comercial"],
                })
                continue

            # Desde la migración a la API, el Excel es SOLO para NEU y Nitro:
            # su dato en `income_statement_data` está malo. Todo lo demás se arma
            # con `POST /panel-contable/cargar-periodo`, que no pide archivos.
            clasif = clasif_map.get(proy["id"], "normal")
            if clasif == "normal":
                resultados["rechazados"].append({
                    "archivo": uf.filename,
                    "proyecto": proy["nombre_comercial"],
                    "clasificacion": clasif,
                    "tipo_carga": tipo_carga,
                    "mensaje": (
                        f"{proy['nombre_comercial']} ya no se carga por Excel: se "
                        f"arma desde la API con «Armar período». El Excel quedó "
                        f"solo para NEU y Nitro. Si esta planta debería ser NEU o "
                        f"Nitro en {periodo_norm}, clasifícala primero."
                    ),
                })
                continue

            # Validación cruzada: la clasificación del período debe coincidir con
            # el tipo de carga elegido.
            if clasif != tipo_carga:
                resultados["rechazados"].append({
                    "archivo": uf.filename,
                    "proyecto": proy["nombre_comercial"],
                    "clasificacion": clasif,
                    "tipo_carga": tipo_carga,
                    "mensaje": (
                        f"{proy['nombre_comercial']} está clasificado como "
                        f"{clasif.upper()} para {periodo_norm}, debe cargarse en "
                        f"su sección correspondiente"
                    ),
                })
                continue

            proyectos_vistos.add(proy["id"])

            recalc_path = recalcular_er(tmp.name)
            parsed = parsear_er(
                recalc_path, tipo=tipo_carga,
                mapeos=mapeos_por_proyecto.get(proy["id"]),
                aliases=aliases_por_proyecto.get(proy["id"]),
                proyecto_nombre=proy["nombre_comercial"],
            )

            with transaction.atomic():
                panel = guardar_panel(
                    proy["id"], periodo_norm, tipo, parsed,
                    er_filename=uf.name, usuario_id=usuario_id,
                )
            resultados["cargados"].append({
                "panel_id": panel.id,
                "proyecto_id": proy["id"],
                "proyecto": proy["nombre_comercial"],
                "archivo": uf.name,
                "ingreso_bruto": float(panel.ingreso_bruto_cop or 0),
            })
            if parsed.get("warnings"):
                resultados["warnings"].append(
                    {"archivo": uf.name, "detalle": parsed["warnings"]})
        except Exception as e:
            # Un ER que falla no tumba la carga de los demás: cada panel se guarda
            # en su propia transacción y este solo se reporta.
            logger.exception("Error procesando ER %s", uf.name)
            resultados["errores"].append({"archivo": uf.name, "error": str(e)})
        finally:
            for p in (tmp.name, recalc_path):
                if p and os.path.exists(p) and p != tmp.name + "__keep":
                    try:
                        os.unlink(p)
                    except Exception:
                        pass

    return {"ok": True, "periodo": periodo_norm, "tipo": tipo, "tipo_carga": tipo_carga, **resultados}


def cargar_periodo(periodo_pedido: str, tipo: str, version: str,
                   usuario_id: int) -> dict:
    """Arma los paneles del período desde la API, sin subir un solo archivo.

    Es el mismo camino que `cargar-er` a partir del `parsed`: cambia de dónde sale
    ese dict, no lo que se hace con él. El reparto por inversionista, los costos de
    los módulos y los impuestos siguen igual.

    NEU y Nitro se omiten a propósito -- su dato en la API está malo y siguen
    cargando el Excel-- y se informan en `omitidos`: saltarlos en silencio haría
    creer que el período quedó completo.
    """
    periodo, anio, mes = _normalizar_periodo(periodo_pedido)

    try:
        er = api_externa.estado_resultados_json(month=mes, year=anio, version=version)
    except api_externa.LiquidacionesAPIError as exc:
        raise ServicioNoDisponible(str(exc))

    # Hereda del período anterior: son plantas estables y un olvido de
    # clasificar no puede hacer que un NEU se arme por el camino de la API.
    clasif = clasificacion_vigente(periodo)
    # La API nombra los proyectos por tópico y algunos difieren del de generación.
    por_topico = {
        (p.topico_liquidaciones or p.sub_project): p
        for p in Proyecto.objects.filter(deleted_at__isnull=True)
        if (p.topico_liquidaciones or p.sub_project)
    }

    armados: list[str] = []
    omitidos: list[dict] = []
    sin_cruce: list[str] = []
    avisos: list[dict] = []

    for proy_api in (er.get("results") or []):
        topico = proy_api.get("project") or ""
        proyecto = por_topico.get(topico)
        if proyecto is None:
            sin_cruce.append(topico)
            continue
        if clasif.get(proyecto.id, "normal") != "normal":
            omitidos.append({"proyecto": proyecto.nombre_comercial,
                             "motivo": clasif[proyecto.id]})
            continue

        parsed = construir_parsed(proy_api)
        with transaction.atomic():
            guardar_panel(proyecto.id, periodo, tipo, parsed, None,
                          usuario_id, origen="api")
        armados.append(proyecto.nombre_comercial)
        if parsed["warnings"]:
            avisos.append({"proyecto": proyecto.nombre_comercial,
                           "avisos": parsed["warnings"]})

    return {
        "periodo": periodo,
        "tipo": tipo,
        "version": version,
        "armados": len(armados),
        "proyectos": armados,
        "omitidos": omitidos,
        "sin_cruce": sin_cruce,
        "avisos": avisos,
        "errores_api": er.get("errors") or [],
    }


def comparar_lineas(excel: list[dict], api: list[dict]) -> list[dict]:
    """Diferencias entre dos juegos de líneas, agrupadas por (grupo, concepto).

    Solo devuelve lo que NO cuadra: una lista vacía significa que la API produce
    exactamente lo mismo que el Excel. Se toleran diferencias menores a un peso,
    que son redondeo y no discrepancia.

    Suma las líneas del mismo concepto porque las del panel vienen divididas por
    inversionista y hay que reagruparlas al 100 % para comparar.
    """
    def _indexar(lineas):
        out: dict[tuple, float] = {}
        for l in lineas:
            clave = (l["grupo"], l["concepto"])
            out[clave] = out.get(clave, 0.0) + float(l.get("valor") or 0)
        return out

    ex, ap = _indexar(excel), _indexar(api)
    diferencias = []
    for clave in sorted(set(ex) | set(ap)):
        v_ex, v_ap = ex.get(clave), ap.get(clave)
        # El lado que falta cuenta como cero. Así una línea en cero que existe de
        # un solo lado no se reporta: no es una diferencia de plata, y con 36
        # proyectos arrastrando conceptos vacíos tapaba las diferencias reales.
        if abs((v_ap or 0) - (v_ex or 0)) < 1:
            continue
        diferencias.append({
            "grupo": clave[0], "concepto": clave[1],
            "excel": v_ex, "api": v_ap,
            "solo_en": "excel" if v_ap is None else ("api" if v_ex is None else None),
            "diferencia": round((v_ap or 0) - (v_ex or 0), 2),
        })
    return diferencias


def contraste_api_vs_excel(periodo: str, tipo: str, version: str) -> dict:
    """En qué se diferencia lo que daría la API de lo que dio el Excel.

    No guarda nada: es para mirar antes de decidir. Las diferencias esperadas son
    la administración de los proyectos GD sin `tarifa_admin`, y FAZNI y cargo por
    confiabilidad, que el Excel no trae. Cualquier otra hay que entenderla antes
    de liquidar con esto.
    """
    periodo, anio, mes = _normalizar_periodo(periodo)

    try:
        er = api_externa.estado_resultados_json(month=mes, year=anio, version=version)
    except api_externa.LiquidacionesAPIError as exc:
        raise ServicioNoDisponible(str(exc))

    api_por_topico = {p["project"]: p for p in (er.get("results") or [])}
    clasif = clasificacion_vigente(periodo)

    paneles = list(
        PanelContable.objects
        .filter(periodo=periodo, tipo=tipo)
        .select_related("proyecto")
        .prefetch_related("lineas")
    )

    salida, cuadran = [], 0
    for panel in paneles:
        proyecto = panel.proyecto
        topico = proyecto.topico_liquidaciones or proyecto.sub_project
        clase = clasif.get(proyecto.id, "normal")

        if clase != "normal":
            salida.append({"proyecto": proyecto.nombre_comercial, "topico": topico,
                           "omitido": clase})
            continue
        proy_api = api_por_topico.get(topico or "")
        if proy_api is None:
            salida.append({"proyecto": proyecto.nombre_comercial, "topico": topico,
                           "sin_dato_en_api": True})
            continue

        parsed = construir_parsed(proy_api)
        diferencias = comparar_lineas(
            excel=[{"grupo": l.grupo, "concepto": l.concepto,
                    "valor": float(l.valor_cop or 0)} for l in panel.lineas.all()],
            api=_construir_lineas_base(parsed),
        )
        if not diferencias:
            cuadran += 1
        salida.append({
            "proyecto": proyecto.nombre_comercial,
            "topico": topico,
            "diferencias": diferencias,
            "avisos": parsed["warnings"],
        })

    return {"periodo": periodo, "tipo": tipo, "version": version,
            "paneles": len(paneles), "cuadran_exacto": cuadran,
            "proyectos": salida}


def estado_resultados_xlsx(panel_id: int, inversionista: str | None = None) -> tuple[bytes, str]:
    """El Estado de Resultados del panel, en Excel.

    La tabla diaria se arma con los dos históricos de la API: los despachos dan
    generación y venta, y el consumo por hora da la importación. Verificado
    contra 2026-07: los totales de la tabla cuadran al peso con los mensuales que
    reporta la API.
    """
    from apps.contabilidad.services.er_diario import construir_tabla_diaria
    from apps.contabilidad.services.er_export import generar_er_xlsx

    panel = (
        PanelContable.objects
        .select_related("proyecto")
        .prefetch_related("lineas")
        .filter(pk=panel_id)
        .first()
    )
    if panel is None:
        raise NotFound("Panel no encontrado")

    proyecto = panel.proyecto
    topico = proyecto.topico_liquidaciones or proyecto.sub_project
    _, anio, mes = _normalizar_periodo(panel.periodo)

    diario: list[dict] = []
    if topico:
        try:
            diario = construir_tabla_diaria(
                despachos=api_externa.listar_liquidaciones_mercado(
                    year=anio, month=mes, project=topico),
                consumos=api_externa.listar_contratos_despachados(
                    year=anio, month=mes, project=topico),
            )
        except api_externa.LiquidacionesAPIError:
            # Los totales del ER salen de las líneas del panel, no de la tabla
            # diaria: que la API no responda no debe impedir descargarlo.
            logger.warning("Sin datos diarios para el ER de %s (%s)", topico, panel.periodo)

    contenido = generar_er_xlsx(panel, proyecto.nombre_comercial, diario, inversionista)
    sufijo = f" - {inversionista}" if inversionista else ""
    return contenido, (
        f"Estado resultados {proyecto.nombre_comercial} {panel.periodo}{sufijo}.xlsx"
    )


def listar_clasificacion(periodo: str) -> dict:
    """
    Todos los proyectos con su tipo de liquidación asignado para el período
    ('normal' por defecto si no tiene registro). La clasificación es POR PERÍODO.
    """
    periodo_norm, _anio, _mes = _normalizar_periodo(periodo)

    # Lo que REALMENTE se va a usar para armar el período, con la herencia
    # aplicada: la vista tiene que mostrar lo mismo que hará el Panel, no solo
    # lo tecleado en ese mes.
    asignados = clasificacion_vigente(periodo_norm)
    # El Panel Contable es SOLO de minigranjas operativas: filtrar para no listar
    # AMC, Acanto, los COLxxx, etc. (mismo filtro que en cargar-er). Además, solo
    # proyectos que REPRESENTAMOS: los que no representamos no se liquidan, así que
    # no deben aparecer en la clasificación (p. ej. San Pedro).
    proyectos = minigranjas_operativas().filter(representamos()).order_by(
        "nombre_comercial")
    return {
        "periodo": periodo_norm,
        "proyectos": [
            {
                "proyecto_id": p.id,
                "proyecto": p.nombre_comercial,
                "tipo": asignados.get(p.id, "normal"),
            }
            for p in proyectos
        ],
    }


def guardar_clasificacion(periodo: str, asignaciones: list[dict]) -> dict:
    """
    Upsert de la clasificación del período. Solo persiste las que difieren de
    'normal' (default); reasignar a 'normal' elimina el registro previo.
    """
    periodo_norm, _anio, _mes = _normalizar_periodo(periodo)

    existentes = {
        c.proyecto_id: c
        for c in ClasificacionLiquidacion.objects.filter(periodo=periodo_norm)
    }
    guardados = 0
    with transaction.atomic():
        for a in asignaciones:
            tipo = (a.get("tipo") or "normal").strip().lower()
            proyecto_id = a["proyecto_id"]
            if tipo not in ("normal", "neu", "nitro"):
                raise NoProcesable(
                    f"tipo inválido para proyecto {proyecto_id}: {a.get('tipo')}")
            actual = existentes.get(proyecto_id)
            if tipo == "normal":
                # 'normal' es el default: no se almacena, se borra el previo.
                if actual is not None:
                    actual.delete()
                    guardados += 1
                continue
            if actual is None:
                ClasificacionLiquidacion.objects.create(
                    proyecto_id=proyecto_id, periodo=periodo_norm, tipo=tipo)
            else:
                actual.tipo = tipo
                actual.save(update_fields=["tipo"])
            guardados += 1

    return {"ok": True, "periodo": periodo_norm, "guardados": guardados}


def _serializar_panel(p: PanelContable, nombres: dict, sop_map: dict | None = None,
                      rates_por_pi: dict | None = None, overrides: dict | None = None,
                      consec_map: dict | None = None) -> dict:
    sop_map = sop_map or {}
    rates_por_pi = rates_por_pi or {}
    overrides = overrides or {}
    # {(proyecto_id, periodo, tipo, inversionista_nombre): (ingresos, costos)}.
    # El consecutivo es por partícipe: el par que vive en el panel solo alcanza
    # para los proyectos de un único inversionista.
    consec_map = consec_map or {}
    def _sop(grupo, concepto):
        return sop_map.get((p.proyecto_id, grupo, concepto))
    # Agrupar líneas por inversionista.
    inv_map: dict = {}
    for ln in sorted(p.lineas.all(), key=lambda x: x.orden):
        key = ln.proyecto_inversionista_id or f"_{ln.inversionista_nombre}"
        if key not in inv_map:
            consec = consec_map.get(
                (p.proyecto_id, p.periodo, p.tipo, ln.inversionista_nombre), (None, None))
            inv_map[key] = {
                "proyecto_inversionista_id": ln.proyecto_inversionista_id,
                "nombre": ln.inversionista_nombre,
                "porcentaje": float(ln.porcentaje) if ln.porcentaje is not None else None,
                "consecutivo_ingresos": consec[0],
                "consecutivo_costos": consec[1],
                "lineas": [],
            }
        base_val = float(ln.valor_cop) if ln.valor_cop is not None else 0.0
        inv_map[key]["lineas"].append({
            "id": ln.id,
            "grupo": ln.grupo,
            "concepto": ln.concepto,
            "valor_cop": base_val,
            "comprobante_contable": ln.comprobante_contable,
            "hoja": ln.hoja,
            "celda": ln.celda,
            # "hoja!celda" listo para mostrar/editar en el frontend (None si es derivado).
            "origen": f"{ln.hoja}!{ln.celda}" if (ln.hoja and ln.celda) else None,
            "fuente": ln.fuente,     # 'om' | 'arriendos' cuando el valor no viene del ER
            "orden": ln.orden,
            "soporte": _sop(ln.grupo, ln.concepto),
        })
        # Desglose de impuestos de la factura de servicio (tiempo de lectura),
        # con excepción por servicio/proyecto si existe.
        _r = rates_por_pi.get(ln.proyecto_inversionista_id) or {}
        _eff = tasas_efectivas(_r, overrides.get((_r.get("cliente_id"), ln.concepto)), p.proyecto_id)
        for imp in impuestos_de_factura(ln.concepto, base_val, _eff):
            inv_map[key]["lineas"].append({
                "id": None, "grupo": "facturas", "concepto": imp["concepto"],
                "valor_cop": imp["valor"], "comprobante_contable": None,
                "hoja": None, "celda": None, "origen": None, "orden": ln.orden,
                "soporte": _sop("facturas", imp["concepto"]), "derivada": True,
            })

    # Vista 100%: valor TOTAL del proyecto por concepto, agregando las líneas ya
    # enriquecidas (base + impuestos) de todos los inversionistas.
    total_100: list[dict] = []
    idx_100: dict = {}
    for inv in inv_map.values():
        for l in inv["lineas"]:
            k = (l["grupo"], l["concepto"])
            if k not in idx_100:
                idx_100[k] = len(total_100)
                total_100.append({
                    "grupo": l["grupo"], "concepto": l["concepto"], "valor_cop": l["valor_cop"],
                    "hoja": l["hoja"], "celda": l["celda"], "origen": l["origen"],
                    "fuente": l.get("fuente"),
                    "comprobante_contable": l["comprobante_contable"], "orden": l["orden"],
                    "soporte": l.get("soporte"), "derivada": l.get("derivada", False),
                })
            else:
                total_100[idx_100[k]]["valor_cop"] += l["valor_cop"]

    return {
        "id": p.id,
        "proyecto_id": p.proyecto_id,
        "proyecto": nombres.get(p.proyecto_id, f"Proyecto {p.proyecto_id}"),
        "periodo": p.periodo,
        "tipo": p.tipo,
        "liquidar": p.liquidar,
        "liquidar_ingresos": p.liquidar_ingresos,
        "liquidar_costos": p.liquidar_costos,
        "generar_mandatos": p.generar_mandatos,
        "tiene_bolsa": p.tiene_bolsa,
        "tiene_costos": p.tiene_costos,
        "comercializador": p.comercializador,
        "ingreso_bruto_cop": float(p.ingreso_bruto_cop) if p.ingreso_bruto_cop is not None else 0.0,
        "fecha_firma": p.fecha_firma.isoformat() if p.fecha_firma else None,
        "consecutivo_ingresos": p.consecutivo_ingresos,
        "consecutivo_costos": p.consecutivo_costos,
        "er_filename": p.er_filename,
        # "er" o "api": con qué se armó. Los costos traen su `fuente` por línea.
        "origen": p.origen,
        "inversionistas": list(inv_map.values()),
        # Vista 100% (total proyecto sin dividir).
        "total_100": total_100,
    }


def listar(periodo: str, tipo: str) -> dict:
    periodo_norm, _anio, _mes = _normalizar_periodo(periodo)

    paneles = list(
        PanelContable.objects
        .filter(periodo=periodo_norm, tipo=tipo)
        .prefetch_related("lineas")
        .order_by("id")
    )

    # Solo los nombres de los proyectos del período (antes cargaba la tabla completa).
    proy_ids = [p.proyecto_id for p in paneles]
    nombres = {}
    if proy_ids:
        nombres = dict(
            Proyecto.objects.filter(id__in=proy_ids)
            .values_list("id", "nombre_comercial")
        )

    # Soportes (archivos Drive) del período/tipo, indexados por (proyecto, grupo, concepto).
    sop_map: dict = {}
    if proy_ids:
        sops = PanelSoporte.objects.filter(
            periodo=periodo_norm, tipo=tipo, proyecto_id__in=proy_ids,
        )
        for s in sops:
            sop_map[(s.proyecto_id, s.grupo, s.concepto)] = {
                "archivo_url": s.archivo_url,
                "archivo_nombre": s.archivo_nombre,
            }

    # Tasas del cliente por proyecto_inversionista (para el desglose de impuestos
    # de las facturas de servicio en tiempo de lectura).
    pi_ids = {
        ln.proyecto_inversionista_id
        for p in paneles for ln in p.lineas.all()
        if ln.proyecto_inversionista_id is not None
    }
    rates_por_pi: dict = {}
    if pi_ids:
        for pi_id, cli_id, iva, ret, rei, ica in (
            ProyectoInversionista.objects
            .filter(id__in=pi_ids)
            .values_list(
                "id", "cliente_id", "cliente__iva_pct", "cliente__retencion_pct",
                "cliente__reteiva_pct", "cliente__reteica_pct",
            )
        ):
            rates_por_pi[pi_id] = {
                "cliente_id": cli_id,
                "iva_pct": float(iva) if iva is not None else None,
                "retencion_pct": float(ret) if ret is not None else None,
                "reteiva_pct": float(rei) if rei is not None else None,
                "reteica_pct": float(ica) if ica is not None else None,
            }
    overrides = overrides_tasa_servicio({r["cliente_id"] for r in rates_por_pi.values()})

    # Consecutivos por partícipe del período, en una sola consulta.
    consec_map = {
        (c.proyecto_id, c.periodo, c.tipo, c.inversionista_nombre):
            (c.consecutivo_ingresos, c.consecutivo_costos)
        for c in PanelConsecutivo.objects.filter(periodo=periodo_norm, tipo=tipo)
    }

    return {
        "periodo": periodo_norm,
        "tipo": tipo,
        "paneles": [_serializar_panel(p, nombres, sop_map, rates_por_pi, overrides, consec_map)
                    for p in paneles],
    }


def _linea_dict(ln) -> dict:
    return {
        "proyecto_inversionista_id": ln.proyecto_inversionista_id,
        "porcentaje": float(ln.porcentaje) if ln.porcentaje is not None else None,
        "valor_cop": float(ln.valor_cop) if ln.valor_cop is not None else 0.0,
        "grupo": ln.grupo, "concepto": ln.concepto,
        "hoja": ln.hoja, "celda": ln.celda, "fuente": ln.fuente,
        "comprobante_contable": ln.comprobante_contable, "orden": ln.orden,
    }


def _reconstruir_base(lineas: list[dict]) -> list[dict]:
    """
    Renglones al 100% (sin dividir) a partir de las líneas YA divididas, usando el
    invariante de generación: valor_cop = base · (porcentaje / 100). Por tanto
    base = Σ valor_cop / Σ (porcentaje / 100) por (grupo, concepto). El invariante
    se cumple en ambas ramas de _inversionistas_de (fracción y 0-100), así que la
    reconstrucción es correcta sin importar con qué escala se generó el snapshot.
    Conserva orden de aparición, celda de origen y comprobante.
    """
    bases: list[dict] = []
    idx: dict = {}
    for ln in sorted(lineas, key=lambda x: x["orden"]):
        k = (ln["grupo"], ln["concepto"])
        if k not in idx:
            idx[k] = len(bases)
            bases.append({
                "grupo": ln["grupo"], "concepto": ln["concepto"],
                "hoja": ln.get("hoja"), "celda": ln.get("celda"),
                "fuente": ln.get("fuente"),
                "comprobante_contable": ln.get("comprobante_contable"),
                "_sum_val": 0.0, "_sum_frac": 0.0,
            })
        b = bases[idx[k]]
        pct = ln.get("porcentaje")
        b["_sum_val"] += ln.get("valor_cop") or 0.0
        b["_sum_frac"] += (pct / 100.0) if pct else 0.0
        if not b.get("comprobante_contable") and ln.get("comprobante_contable"):
            b["comprobante_contable"] = ln["comprobante_contable"]
    for b in bases:
        # Σfrac ≈ 0 (todos los % en 0/None) ⇒ no se puede des-dividir; se deja el
        # valor tal cual para no inventar una base.
        b["valor"] = (b["_sum_val"] / b["_sum_frac"]) if b["_sum_frac"] else b["_sum_val"]
    return bases


def _redividir_lineas(lineas: list[dict], invs: list[dict]) -> list[dict]:
    """
    Reparte de nuevo las líneas por inversionista con los % de `invs` (mismo formato
    que _inversionistas_de). Replica el orden de _guardar_panel (inversionista
    externo, concepto interno). Idempotente si los % no cambian.
    """
    bases = _reconstruir_base(lineas)
    out: list[dict] = []
    orden = 0
    for inv in invs:
        frac = inv["fraccion"] if inv["fraccion"] is not None else 0.0
        for b in bases:
            out.append({
                "proyecto_inversionista_id": inv["id"],
                "inversionista_nombre": inv["nombre"],
                "porcentaje": inv["pct"],
                "grupo": b["grupo"], "concepto": b["concepto"],
                "valor_cop": round(b["valor"] * frac, 2),
                "hoja": b["hoja"], "celda": b["celda"], "fuente": b.get("fuente"),
                "comprobante_contable": b["comprobante_contable"],
                "orden": orden,
            })
            orden += 1
    return out


def _division_desactualizada(lineas: list[dict], invs: list[dict], tol: float = 0.01) -> bool:
    """
    True si el % por inversionista guardado en las líneas NO coincide con el correcto
    (invs). Evita repisar paneles sanos o con ediciones manuales: solo se re-divide
    cuando el reparto está realmente mal escalado o cambió la composición.
    """
    correcto = {i["id"]: i["pct"] for i in invs if i["id"] is not None and i["pct"] is not None}
    if not correcto:
        return False
    guardado: dict = {}
    for ln in lineas:
        pid = ln.get("proyecto_inversionista_id")
        if pid is not None and ln.get("porcentaje") is not None:
            guardado[pid] = float(ln["porcentaje"])
    if set(guardado) != set(correcto):
        return True
    return any(abs(guardado[k] - correcto[k]) > tol for k in correcto)


def redividir(periodo: str, tipo: str, proyecto_id: int | None = None,
              forzar: bool = False) -> dict:
    """Vuelve a repartir las líneas ya guardadas con los porcentajes ACTUALES.

    Las líneas de un panel son un SNAPSHOT dividido al momento de cargar el ER. Si
    el panel se generó con un % mal escalado —una versión vieja trataba la
    fracción 1.0 como "1 %", dando valores 100 veces menores—, el listado sigue
    sirviendo ese snapshot y recargar el ER era el único refresco, dependiendo de
    LibreOffice en el servidor. Esto reconstruye la base al 100 % desde las
    propias líneas y la vuelve a repartir, sin archivo ni recálculo.

    Idempotente: salta los paneles cuyo % ya coincide, preservando ediciones,
    salvo `forzar=True`.
    """
    periodo_norm, _anio, _mes = _normalizar_periodo(periodo)

    qs = PanelContable.objects.filter(
        periodo=periodo_norm, tipo=tipo,
    ).prefetch_related("lineas")
    if proyecto_id is not None:
        qs = qs.filter(proyecto_id=proyecto_id)
    paneles = list(qs.order_by("id"))

    # Batch: todos los inversionistas de los proyectos del período en una query
    # (antes era _inversionistas_de por panel → N+1).
    invs_por_proy = _inversionistas_de_batch(
        [p.proyecto_id for p in paneles], periodo_norm
    )

    redivididos, saltados = [], []
    for panel in paneles:
        lineas = [_linea_dict(ln) for ln in panel.lineas.all()]
        if not lineas:
            saltados.append({"panel_id": panel.id, "proyecto_id": panel.proyecto_id, "motivo": "sin_lineas"})
            continue
        invs = invs_por_proy.get(panel.proyecto_id) or []
        if not invs:
            invs = [{"id": None, "nombre": "Sin inversionistas", "fraccion": 1.0, "pct": 100.0}]
        if not forzar and not _division_desactualizada(lineas, invs):
            saltados.append({"panel_id": panel.id, "proyecto_id": panel.proyecto_id, "motivo": "ya_correcto"})
            continue
        nuevas = _redividir_lineas(lineas, invs)
        with transaction.atomic():
            PanelContableLinea.objects.filter(panel_id=panel.id).delete()
            PanelContableLinea.objects.bulk_create(
                [PanelContableLinea(panel_id=panel.id, **nl) for nl in nuevas]
            )
        redivididos.append({
            "panel_id": panel.id, "proyecto_id": panel.proyecto_id,
            "lineas": len(nuevas),
            "porcentajes": sorted({round(i["pct"], 4) for i in invs if i["pct"] is not None}),
        })
    return {
        "ok": True, "periodo": periodo_norm, "tipo": tipo,
        "n_redivididos": len(redivididos), "n_saltados": len(saltados),
        "redivididos": redivididos, "saltados": saltados,
    }


# ── Soportes en Drive ────────────────────────────────────────────────────────────

SOPORTE_MAX = 20 * 1024 * 1024  # 20 MB


def _panel_o_404(panel_id: int) -> PanelContable:
    panel = PanelContable.objects.filter(id=panel_id).first()
    if not panel:
        raise NotFound("Panel no encontrado")
    return panel


def subir_soporte(panel_id: int, grupo: str, concepto: str, archivo, usuario) -> dict:
    """Sube un soporte a Drive y lo ancla a (proyecto, periodo, tipo, grupo, concepto).

    El ancla NO es la línea: sobrevive a que se recargue el ER y se reconstruyan
    las líneas con ids nuevos.
    """
    from apps.comun import drive_evidencia

    panel = _panel_o_404(panel_id)

    contenido = archivo.read()
    if len(contenido) > SOPORTE_MAX:
        raise NoProcesable("El archivo supera el límite de 20 MB")

    import io

    from googleapiclient.http import MediaIoBaseUpload

    proy_nombre = (
        Proyecto.objects.filter(id=panel.proyecto_id)
        .values_list("nombre_comercial", flat=True).first()
        or f"Proyecto {panel.proyecto_id}"
    )
    try:
        service = drive_evidencia.servicio()
        proy_folder = drive_evidencia.carpeta(
            service, proy_nombre, drive_evidencia.CARPETA_RAIZ
        )
        panel_folder = drive_evidencia.carpeta(
            service, f"Panel {panel.periodo} {panel.tipo}", proy_folder
        )
    except drive_evidencia.DriveNoConfigurado:
        raise
    except Exception as e:
        raise ServicioNoDisponible(f"Error accediendo a Drive: {e}")

    nombre = getattr(archivo, "name", None) or f"soporte_{concepto}"
    media = MediaIoBaseUpload(
        io.BytesIO(contenido),
        mimetype=getattr(archivo, "content_type", None) or "application/octet-stream",
    )
    try:
        up = service.files().create(
            body={"name": nombre, "parents": [panel_folder]},
            media_body=media, fields="id, webViewLink", supportsAllDrives=True,
        ).execute()
    except Exception as e:
        raise ServicioNoDisponible(f"Error subiendo a Drive: {e}")

    file_id = up["id"]
    url = up.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view")

    PanelSoporte.objects.update_or_create(
        proyecto_id=panel.proyecto_id, periodo=panel.periodo, tipo=panel.tipo,
        grupo=grupo, concepto=concepto,
        defaults={
            "archivo_url": url, "archivo_nombre": nombre, "drive_file_id": file_id,
            "created_by_id": getattr(usuario, "id", None),
        },
    )
    return {"grupo": grupo, "concepto": concepto,
            "archivo_url": url, "archivo_nombre": nombre}


def eliminar_soporte(panel_id: int, grupo: str, concepto: str) -> dict:
    """Quita el soporte de (grupo, concepto). El archivo queda en Drive."""
    panel = _panel_o_404(panel_id)
    PanelSoporte.objects.filter(
        proyecto_id=panel.proyecto_id, periodo=panel.periodo, tipo=panel.tipo,
        grupo=grupo, concepto=concepto,
    ).delete()
    return {"ok": True}


# ── Edición del panel y de sus líneas ────────────────────────────────────────────

CAMPOS_PATCH = (
    "liquidar", "liquidar_ingresos", "liquidar_costos", "generar_mandatos",
    "fecha_firma", "consecutivo_ingresos", "consecutivo_costos",
)


def _nombres_de(panel: PanelContable) -> dict:
    return {
        panel.proyecto_id: Proyecto.objects.filter(id=panel.proyecto_id)
        .values_list("nombre_comercial", flat=True).first()
    }


def actualizar(panel_id: int, datos: dict) -> dict:
    panel = _panel_o_404(panel_id)

    for campo in CAMPOS_PATCH:
        val = datos.get(campo)
        if val is not None:
            setattr(panel, campo, val)

    lineas_patch = datos.get("lineas") or []
    with transaction.atomic():
        panel.save(update_fields=list(CAMPOS_PATCH))
        if lineas_patch:
            por_id = {l["id"]: l for l in lineas_patch}
            lineas = PanelContableLinea.objects.filter(
                id__in=list(por_id), panel_id=panel_id,
            )
            for ln in lineas:
                patch = por_id[ln.id]
                for campo in ("valor_cop", "comprobante_contable", "concepto"):
                    if patch.get(campo) is not None:
                        setattr(ln, campo, patch[campo])
                ln.save(update_fields=["valor_cop", "comprobante_contable", "concepto"])

    panel.refresh_from_db()
    return _serializar_panel(panel, _nombres_de(panel))


# ── Mapeo de celda por concepto (PROPONER → CORREGIR → RECORDAR) ─────────────────

def guardar_mapeo_celda(proyecto_id: int, periodo: str, tipo: str, concepto: str,
                        hoja: str, celda: str) -> dict:
    """La usuaria corrige la celda de origen de un concepto ("hoja!celda").

    Relee esa celda del snapshot del ER, guarda el mapeo por (proyecto, concepto)
    para los próximos meses (RECORDAR) y actualiza el valor de las líneas de ese
    concepto, re-dividido por el % de cada inversionista.
    """
    from apps.contabilidad.services.er_loader import _aplicar_signo, leer_celda

    periodo_norm, _anio, _mes = _normalizar_periodo(periodo)

    hoja = (hoja or "").strip()
    celda = (celda or "").strip().upper().replace("$", "")
    if not hoja or not celda:
        raise NoProcesable("Debe indicar hoja y celda (ej. Sheet1 / H35)")

    panel = PanelContable.objects.filter(
        proyecto_id=proyecto_id, periodo=periodo_norm, tipo=tipo,
    ).first()
    if not panel:
        raise NotFound("No hay panel para ese proyecto/período/tipo")
    if not panel.er_snapshot:
        raise Conflict(
            "El panel no tiene snapshot del ER (vuelve a cargar el ER para poder "
            "remapear celdas)"
        )

    val = leer_celda(json.loads(panel.er_snapshot), hoja, celda)
    if val is None:
        raise NoProcesable(f"{hoja}!{celda} no tiene un valor numérico en el ER")

    lineas = list(PanelContableLinea.objects.filter(
        panel_id=panel.id, concepto=concepto,
    ))
    if not lineas:
        raise NotFound(f"El panel no tiene el concepto '{concepto}'")

    with transaction.atomic():
        MapeoCeldaConcepto.objects.update_or_create(
            proyecto_id=proyecto_id, concepto=concepto,
            defaults={"hoja": hoja, "celda": celda},
        )
        for ln in lineas:
            base = _aplicar_signo(ln.grupo, ln.concepto, val)
            frac = (float(ln.porcentaje) / 100.0) if ln.porcentaje is not None else 1.0
            ln.valor_cop = round(base * frac, 2)
            ln.hoja = hoja
            ln.celda = celda
            ln.save(update_fields=["valor_cop", "hoja", "celda"])

    panel.refresh_from_db()
    return _serializar_panel(panel, _nombres_de(panel))


# ── Fuentes de ingreso: alias persistente + agregar/quitar ───────────────────────

def _split_columna_origen(s: str) -> tuple[str, str]:
    """'Sheet1!G35' → ('Sheet1', 'G35'). 422 si el formato es inválido."""
    if not s or "!" not in s:
        raise NoProcesable("columna_origen debe ser 'hoja!celda' (ej. Sheet1!G35)")
    hoja, celda = s.split("!", 1)
    hoja = hoja.strip()
    celda = celda.strip().upper().replace("$", "")
    if not hoja or not celda:
        raise NoProcesable("columna_origen debe ser 'hoja!celda' (ej. Sheet1!G35)")
    return hoja, celda


def _panel_para(proyecto_id: int, periodo: str, tipo: str) -> PanelContable:
    periodo_norm, _anio, _mes = _normalizar_periodo(periodo)
    panel = PanelContable.objects.filter(
        proyecto_id=proyecto_id, periodo=periodo_norm, tipo=tipo,
    ).first()
    if not panel:
        raise NotFound("No hay panel para ese proyecto/período/tipo")
    return panel


def _panel_serializado(panel: PanelContable) -> dict:
    panel.refresh_from_db()
    return _serializar_panel(panel, _nombres_de(panel))


def guardar_alias_fuente(proyecto_id: int, periodo: str, tipo: str,
                         columna_origen: str, etiqueta: str,
                         orden: int | None = None) -> dict:
    """Renombra una fuente de ingreso, anclada a su celda de origen.

    Guarda el alias (RECORDAR, para el próximo mes), relee el valor de esa celda
    del snapshot y renombra/actualiza las líneas de ingreso de esa fuente.
    """
    from apps.contabilidad.services.er_loader import _aplicar_signo, leer_celda

    hoja, celda = _split_columna_origen(columna_origen)
    col = f"{hoja}!{celda}"
    panel = _panel_para(proyecto_id, periodo, tipo)

    val = (
        leer_celda(json.loads(panel.er_snapshot), hoja, celda)
        if panel.er_snapshot else None
    )
    with transaction.atomic():
        alias, creado = AliasFuenteIngreso.objects.get_or_create(
            proyecto_id=proyecto_id, columna_origen=col,
            defaults={"etiqueta": etiqueta, "orden": orden or 0},
        )
        if not creado:
            alias.etiqueta = etiqueta
            if orden is not None:
                alias.orden = orden
            alias.save(update_fields=["etiqueta", "orden"])

        for ln in PanelContableLinea.objects.filter(
            panel_id=panel.id, grupo="ingresos", celda=celda,
        ):
            if (ln.hoja or "").lower() != hoja.lower():
                continue
            ln.concepto = etiqueta
            if val is not None:
                base = _aplicar_signo("ingresos", etiqueta, val)
                frac = (float(ln.porcentaje) / 100.0) if ln.porcentaje is not None else 1.0
                ln.valor_cop = round(base * frac, 2)
            ln.save(update_fields=["concepto", "valor_cop"])

    return _panel_serializado(panel)


def agregar_fuente_ingreso(proyecto_id: int, periodo: str, tipo: str, hoja: str,
                           celda: str, etiqueta: str, orden: int | None = None) -> dict:
    """Agrega a mano una fuente de ingreso que el parser no detectó (ej. un PPA).

    Lee el valor de la celda del snapshot, crea una línea por inversionista y
    guarda el alias para que reaparezca el próximo mes.
    """
    from django.db.models import Max

    from apps.contabilidad.services.er_loader import _aplicar_signo, leer_celda

    hoja = (hoja or "").strip()
    celda = (celda or "").strip().upper().replace("$", "")
    if not hoja or not celda:
        raise NoProcesable("Debe indicar hoja y celda (ej. Sheet1 / G35)")
    panel = _panel_para(proyecto_id, periodo, tipo)
    if not panel.er_snapshot:
        raise Conflict("El panel no tiene snapshot del ER (recarga el ER)")
    val = leer_celda(json.loads(panel.er_snapshot), hoja, celda)
    if val is None:
        raise NoProcesable(f"{hoja}!{celda} no tiene un valor numérico en el ER")

    col = f"{hoja}!{celda}"
    orden = orden if orden is not None else 0

    base = _aplicar_signo("ingresos", etiqueta, val)
    invs = _inversionistas_de(proyecto_id, panel.periodo)
    if not invs:
        invs = [{"id": None, "nombre": "Sin inversionistas", "fraccion": 1.0, "pct": 100.0}]
    orden_max = PanelContableLinea.objects.filter(panel_id=panel.id).aggregate(
        m=Max("orden"))["m"] or 0

    nuevas = []
    for inv in invs:
        frac = inv["fraccion"] if inv["fraccion"] is not None else 0.0
        orden_max += 1
        nuevas.append(PanelContableLinea(
            panel_id=panel.id, proyecto_inversionista_id=inv["id"],
            inversionista_nombre=inv["nombre"], porcentaje=inv["pct"],
            grupo="ingresos", concepto=etiqueta,
            valor_cop=round(base * frac, 2), hoja=hoja, celda=celda, orden=orden_max,
        ))

    with transaction.atomic():
        AliasFuenteIngreso.objects.update_or_create(
            proyecto_id=proyecto_id, columna_origen=col,
            defaults={"etiqueta": etiqueta, "orden": orden},
        )
        PanelContableLinea.objects.bulk_create(nuevas)

    return _panel_serializado(panel)


def quitar_fuente_ingreso(proyecto_id: int, periodo: str, tipo: str,
                          columna_origen: str) -> dict:
    """Quita una fuente de ingreso (sus líneas por inversionista) y su alias."""
    hoja, celda = _split_columna_origen(columna_origen)
    panel = _panel_para(proyecto_id, periodo, tipo)

    with transaction.atomic():
        borradas, _ = PanelContableLinea.objects.filter(
            panel_id=panel.id, grupo="ingresos", celda=celda,
        ).delete()
        AliasFuenteIngreso.objects.filter(
            proyecto_id=proyecto_id, columna_origen=f"{hoja}!{celda}",
        ).delete()

    out = _panel_serializado(panel)
    out["lineas_borradas"] = borradas
    return out


# ── Reasignar consecutivos en cadena ─────────────────────────────────────────────

def _asignar_consecutivos(
    paneles: list[PanelContable], ini_ing: int, ini_cos: int, solo_faltantes: bool,
    ocup_ing_extra: set | None = None, ocup_cos_extra: set | None = None,
) -> list[dict]:
    """Numera (in-place, sin guardar) las dos cadenas de consecutivos.

    Ver `reasignar_consecutivos` para la semántica de `solo_faltantes`.
    ocup_*_extra: números ya usados en otros períodos (unicidad global por cadena).
    """
    extras = {"consecutivo_ingresos": ocup_ing_extra or set(),
              "consecutivo_costos": ocup_cos_extra or set()}

    def _cadena(activo, attr, inicio):
        # Ocupados: siempre los de otros períodos (unicidad global) + en modo
        # rellenar, también los ya asignados de este período (no pisar ediciones).
        ocupados = set(extras[attr])
        if solo_faltantes:
            ocupados |= {
                getattr(p, attr) for p in paneles
                if activo(p) and getattr(p, attr) is not None
            }
        siguiente = inicio

        def _libre():
            nonlocal siguiente
            while siguiente in ocupados:
                siguiente += 1
            n = siguiente
            ocupados.add(n)
            siguiente += 1
            return n

        for p in paneles:
            if not activo(p):
                # No liquida esa cadena → siempre se limpia (no debe tener consecutivo).
                setattr(p, attr, None)
                continue
            if solo_faltantes:
                if getattr(p, attr) is None:
                    setattr(p, attr, _libre())
            else:
                setattr(p, attr, _libre())

    _cadena(lambda p: bool(p.liquidar_ingresos), "consecutivo_ingresos", ini_ing)
    # Costos: se respeta la decisión del usuario (liquidar_costos), sin atarla a si
    # el ER trajo líneas de costos: un proyecto puede tener costos que este mes no
    # llegaron en el ER o que vendrán de la vista de costos. El default de paneles
    # sin costos sigue siendo liquidar_costos=False, así que solo se numeran los
    # que el usuario marca explícitamente.
    _cadena(lambda p: bool(p.liquidar_costos), "consecutivo_costos", ini_cos)

    return [
        {
            "panel_id": p.id,
            "consecutivo_ingresos": p.consecutivo_ingresos,
            "consecutivo_costos": p.consecutivo_costos,
        }
        for p in paneles
    ]


def reasignar_consecutivos(periodo: str, tipo: str, consecutivo_ingresos_inicial: int,
                           consecutivo_costos_inicial: int,
                           solo_faltantes: bool = False) -> dict:
    """Asigna consecutivos en dos cadenas independientes:

      - Ingresos: a cada panel con liquidar_ingresos=true.
      - Costos: a cada panel con liquidar_costos=true (decisión del usuario; no se
        exige que el ER haya traído líneas de costos).

    Dos modos (`solo_faltantes`):
      - False (renumerar): reasigna TODO desde el valor inicial, en orden de id.
      - True  (rellenar):  preserva los consecutivos ya asignados (incl. ediciones
        manuales) y solo numera los que están en None, tomando el menor número libre
        ≥ inicial. Así todo panel marcado queda numerado sin pisar lo editado.
    """
    periodo_norm, _anio, _mes = _normalizar_periodo(periodo)

    # Los consecutivos son SOLO del oficial (la preliquidación no lleva; el mandato
    # oficial = la diferencia). Para cualquier otro tipo, no se numera.
    if tipo != "oficial":
        return {"ok": True, "solo_faltantes": solo_faltantes, "asignados": [],
                "omitido": "solo_oficial"}

    paneles = list(
        PanelContable.objects.filter(periodo=periodo_norm, tipo="oficial").order_by("id")
    )
    # Unicidad GLOBAL por cadena: números ya usados por paneles oficiales de OTROS
    # períodos, que esta reasignación debe respetar (no repetir).
    otros = PanelContable.objects.filter(tipo="oficial").exclude(
        periodo=periodo_norm
    ).values_list("consecutivo_ingresos", "consecutivo_costos")
    ocup_ing = {r[0] for r in otros if r[0] is not None}
    ocup_cos = {r[1] for r in otros if r[1] is not None}

    asignados = _asignar_consecutivos(
        paneles, consecutivo_ingresos_inicial, consecutivo_costos_inicial,
        solo_faltantes=solo_faltantes,
        ocup_ing_extra=ocup_ing, ocup_cos_extra=ocup_cos,
    )
    with transaction.atomic():
        for p in paneles:
            p.save(update_fields=["consecutivo_ingresos", "consecutivo_costos"])
    return {"ok": True, "solo_faltantes": solo_faltantes, "asignados": asignados}


def consecutivos_usados(excluir_panel_id: int | None = None) -> dict:
    """Consecutivos ya usados por paneles OFICIALES (unicidad global por cadena).

    Para que el frontend avise de duplicados y sugiera el siguiente libre. Los
    consecutivos son solo del oficial (la preliquidación no lleva).
    `excluir_panel_id`: el panel en edición, para que no choque consigo mismo.
    """
    filas = PanelContable.objects.filter(tipo="oficial").values_list(
        "id", "consecutivo_ingresos", "consecutivo_costos"
    )
    ing: dict[int, int] = {}
    cos: dict[int, int] = {}
    for pid, ci, cc in filas:
        if pid == excluir_panel_id:
            continue
        if ci is not None:
            ing[ci] = pid
        if cc is not None:
            cos[cc] = pid

    def cadena(uso: dict[int, int]) -> dict:
        return {
            "usados": sorted(uso.keys()),
            "siguiente": (max(uso.keys()) + 1) if uso else 1,
            "por_numero": {str(k): v for k, v in uso.items()},
        }

    return {"ingresos": cadena(ing), "costos": cadena(cos)}


# ── Diferencia preliquidación vs oficial ────────────────────────────────────────

def diferencia(periodo: str) -> dict:
    """Preliquidación vs oficial del período, línea a línea y por inversionista."""
    periodo_norm, _anio, _mes = _normalizar_periodo(periodo)

    paneles = list(
        PanelContable.objects.filter(periodo=periodo_norm).prefetch_related("lineas")
    )
    nombres = dict(Proyecto.objects.values_list("id", "nombre_comercial"))

    # Indexar paneles por proyecto y tipo.
    pre_por_proy: dict[int, PanelContable] = {}
    ofi_por_proy: dict[int, PanelContable] = {}
    for p in paneles:
        if p.tipo == "preliquidacion":
            pre_por_proy[p.proyecto_id] = p
        elif p.tipo == "oficial":
            ofi_por_proy[p.proyecto_id] = p

    tiene_oficial = bool(ofi_por_proy)
    tiene_preliquidacion = bool(pre_por_proy)

    def _inv_key(ln: PanelContableLinea):
        return ln.proyecto_inversionista_id if ln.proyecto_inversionista_id is not None \
            else f"_{ln.inversionista_nombre}"

    def _indexar_inversionistas(panel: PanelContable | None) -> dict:
        """inv_key → {nombre, porcentaje, orden, lineas: {(grupo,concepto): {valor, orden}}}"""
        out: dict = {}
        if panel is None:
            return out
        for ln in sorted(panel.lineas.all(), key=lambda x: x.orden):
            k = _inv_key(ln)
            inv = out.setdefault(k, {
                "nombre": ln.inversionista_nombre,
                "porcentaje": float(ln.porcentaje) if ln.porcentaje is not None else None,
                "orden": len(out),
                "lineas": {},
            })
            inv["lineas"][(ln.grupo, ln.concepto)] = {
                "valor": float(ln.valor_cop) if ln.valor_cop is not None else 0.0,
                "orden": ln.orden,
            }
        return out

    proyectos_out = []
    tot_pre_global = tot_ofi_global = 0.0
    proyecto_ids = sorted(set(pre_por_proy) | set(ofi_por_proy),
                          key=lambda pid: (pre_por_proy.get(pid) or ofi_por_proy.get(pid)).id)

    for pid in proyecto_ids:
        pre_panel = pre_por_proy.get(pid)
        ofi_panel = ofi_por_proy.get(pid)
        hay_ofi = ofi_panel is not None
        hay_pre = pre_panel is not None

        pre_invs = _indexar_inversionistas(pre_panel)
        ofi_invs = _indexar_inversionistas(ofi_panel)

        # Unión de inversionistas, ordenada por aparición en pre y luego oficial.
        inv_keys = list(pre_invs.keys())
        for k in ofi_invs:
            if k not in inv_keys:
                inv_keys.append(k)

        inversionistas_out = []
        u_pre_proy = u_ofi_proy = 0.0
        for k in inv_keys:
            pi = pre_invs.get(k)
            oi = ofi_invs.get(k)
            nombre = (pi or oi)["nombre"]
            porcentaje = (pi or oi)["porcentaje"]

            # Unión de líneas (grupo, concepto), preservando el orden de pre y
            # añadiendo las que solo existan en oficial.
            claves = []
            vistos = set()
            for src in ((pi["lineas"] if pi else {}), (oi["lineas"] if oi else {})):
                for clave, meta in sorted(src.items(), key=lambda kv: kv[1]["orden"]):
                    if clave not in vistos:
                        vistos.add(clave)
                        claves.append(clave)

            lineas_out = []
            u_pre = u_ofi = 0.0
            for (grupo, concepto) in claves:
                # Si falta uno de los dos paneles, esa columna va en null (no 0).
                if hay_pre:
                    pre_v = pi["lineas"][(grupo, concepto)]["valor"] if (pi and (grupo, concepto) in pi["lineas"]) else 0.0
                else:
                    pre_v = None
                if hay_ofi:
                    ofi_v = oi["lineas"][(grupo, concepto)]["valor"] if (oi and (grupo, concepto) in oi["lineas"]) else 0.0
                else:
                    ofi_v = None
                dif = (ofi_v - pre_v) if (pre_v is not None and ofi_v is not None) else None
                pct = (dif / abs(pre_v) * 100) if (dif is not None and pre_v) else None
                if pre_v is not None:
                    u_pre += pre_v
                if ofi_v is not None:
                    u_ofi += ofi_v
                lineas_out.append({
                    "grupo": grupo,
                    "concepto": concepto,
                    "preliquidacion": round(pre_v, 2) if pre_v is not None else None,
                    "oficial": round(ofi_v, 2) if ofi_v is not None else None,
                    "diferencia": round(dif, 2) if dif is not None else None,
                    "pct_variacion": round(pct, 2) if pct is not None else None,
                })

            u_dif = (u_ofi - u_pre) if (hay_pre and hay_ofi) else None
            inversionistas_out.append({
                "proyecto_inversionista_id": None if isinstance(k, str) else k,
                "nombre": nombre,
                "porcentaje": porcentaje,
                "lineas": lineas_out,
                "utilidad_pre": round(u_pre, 2) if hay_pre else None,
                "utilidad_oficial": round(u_ofi, 2) if hay_ofi else None,
                "utilidad_dif": round(u_dif, 2) if u_dif is not None else None,
            })
            if hay_pre:
                u_pre_proy += u_pre
            if hay_ofi:
                u_ofi_proy += u_ofi

        if hay_pre:
            tot_pre_global += u_pre_proy
        if hay_ofi:
            tot_ofi_global += u_ofi_proy

        proyectos_out.append({
            "proyecto_id": pid,
            "proyecto_nombre": nombres.get(pid, f"Proyecto {pid}"),
            "tiene_preliquidacion": hay_pre,
            "tiene_oficial": hay_ofi,
            "utilidad_pre": round(u_pre_proy, 2) if hay_pre else None,
            "utilidad_oficial": round(u_ofi_proy, 2) if hay_ofi else None,
            "utilidad_dif": round(u_ofi_proy - u_pre_proy, 2) if (hay_pre and hay_ofi) else None,
            "inversionistas": inversionistas_out,
        })

    return {
        "periodo": periodo_norm,
        "tiene_preliquidacion": tiene_preliquidacion,
        "tiene_oficial": tiene_oficial,
        "proyectos": proyectos_out,
        "resumen": {
            "utilidad_estimada": round(tot_pre_global, 2) if tiene_preliquidacion else None,
            "utilidad_real": round(tot_ofi_global, 2) if tiene_oficial else None,
            "diferencia": round(tot_ofi_global - tot_pre_global, 2) if (tiene_preliquidacion and tiene_oficial) else None,
        },
    }
