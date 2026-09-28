"""Backfill de los contratos unificados (D-10) y sus tarifas versionadas (D-24).

Llena `contratos`, `contrato_partes`, `contrato_proyectos` y `contrato_tarifas` desde
las tablas vivas `ppa_contratos`/`ppa_tarifas`/`ppa_contrato_proyectos` y
`contratos_servicio`. FASE ADITIVA: las tablas viejas siguen siendo la fuente de verdad
de todos los lectores; este comando solo puebla las nuevas.

Se corre UNA vez contra producción y se BORRA (CLAUDE.md: los datos nunca van dentro de
una migración de Django). En el servidor:

    docker compose exec operaciones python manage.py backfill_contratos_unificados --dry-run
    docker compose exec operaciones python manage.py backfill_contratos_unificados

Idempotente vía `--reset` (borra las 4 tablas nuevas antes de recargar). `--dry-run` hace
todo el trabajo dentro de una transacción y la revierte, imprimiendo el reporte.

Reglas heredadas del diseño (docs/refactor):
- La fila base se expresa con `origen='pactada'` (no hay columna `es_base`).
- Regla 2: nunca se crea una tarifa con valor 0.0 / NULL; se registra en `omitidas_por_cero`.
  Los contratos 62/69 (cgm=0 con representación real) y el grupo a (54,115,197,209,210)
  caen acá: omitir el cero es la opción reversible mientras Juan/Jessica deciden.
- Las partes se resuelven contra `clientes` por id, nit o razón social; NUNCA se inventan.
- El aniversario (mes/día) sale de fecha_firma/fecha_inicio con default 1/1, igual que
  `apps.contabilidad.services.costos._tarifa_indexada_periodo`, para que la vigencia dé la
  misma respuesta que el código vivo.
"""
from __future__ import annotations

import datetime as dt
import unicodedata
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.backends.postgresql.psycopg_any import DateRange

from apps.clientes.models import Cliente
from apps.contratos.models import (
    Contrato,
    ContratoParte,
    ContratoProyecto,
    ContratoRol,
    ContratoTarifa,
    ContratoTipo,
    EstadoContrato,
    TarifaConcepto,
    TarifaOrigen,
    TarifaUnidad,
    TipoContrato,
)

# Las tablas viejas (ppa_contratos, contratos_servicio, …) ya NO tienen modelo propio:
# PpaContrato/ContratoServicio son fachadas proxy sobre `contratos`. Por eso el copiado
# lee las tablas fuente por SQL crudo (ver `_leer`), no por ORM.


class _Rollback(Exception):
    """Señal interna para revertir la transacción en --dry-run."""


# servicio_aplica -> conjunto de tipos del contrato. Un contrato puede tener varios:
# operación y mantenimiento son un solo contrato (O&M) con dos tipos. Para
# representación/CGM los tipos se derivan de qué tarifas están cargadas (ver
# `_tipos_servicio`), porque representación y CGM comparten fila.
TIPOS_POR_SERVICIO = {
    "mantenimiento": {TipoContrato.OPERACION, TipoContrato.MANTENIMIENTO},
    "arriendo": {TipoContrato.ARRIENDO},
    "internet": {TipoContrato.INTERNET},
}

# Columnas (tabla, columna) que hoy son FK a ppa_contratos.id y hay que re-apuntar a
# contratos.id con el mapa viejo->nuevo. `ppa_contrato_proyectos` NO va acá: lo
# reemplaza contrato_proyectos, y se elimina.
REMAP_PPA = [
    ("ppa_tarifas", "contrato_id"),
    ("ppa_compromisos_energia", "contrato_id"),
    ("oportunidad_ofertas", "ppa_contrato_id"),
    ("cliente_documentos_comerciales", "ppa_contrato_id"),
    ("alertas", "ppa_id"),
    ("asic_solicitudes", "contrato_ppa_id"),
    ("cumplimiento_mensual", "contrato_ppa_id"),
    ("clasificacion_energia_mensual", "contrato_ppa_id"),
]
# Columnas que hoy son FK a contratos_servicio.id.
REMAP_SRV = [
    ("oportunidad_ofertas", "contrato_servicio_id"),
    ("contrato_factura", "contrato_id"),
    ("arr_arrendador", "contrato_id"),
    ("contrato_frontera", "contrato_servicio_id"),
    ("cliente_documentos_comerciales", "contrato_servicio_id"),
    ("om_seleccion_mensual", "contrato_id"),
    ("om_pagina_sin_match", "contrato_id_asignado"),
    ("om_documento_proyecto", "contrato_id"),
    ("contrato_alertas_aniversario", "contrato_id"),
]

# Clientes que existen en `clientes` con OTRO formato de nombre (tilde/punto/&/nombre
# largo). Mapea nombre-normalizado-del-contrato -> nombre-normalizado-del-cliente-real,
# para los casos que la normalización sola no alcanza. Verificado contra la base 2026-09.
ALIAS_CLIENTE = {
    "sonetel s.a.s": "soluciones de energia y telecomunicaciones sonetel s.a.s",
    "sol y cielo s.a.s": "sol y cielo energia s.a.s e.s.p",
}

# Clientes que NO existen en `clientes` y hay que crear (nombre, nit). El backfill los
# crea (get_or_create) antes de resolver las partes. Verificado contra la base 2026-09.
CLIENTES_A_CREAR = [
    ("NEU I S.A.S. E.S.P.", None),
    ("NEU II S.A.S. E.S.P.", None),
    ("BEAM ENERGY INNOVATION S.A.S. E.S.P.", None),
    ("LUMINA ENERGY S.A.S. E.S.P.", None),
    ("ENERMAS S.A.S. E.S.P.", None),
    ("Bia Energy S.A.S.", "901588412"),
    ("NITRO ENERGY COLOMBIA S A S E S P", "900691280"),
]


def _norm(nombre) -> str:
    """Normaliza una razón social para comparar: minúsculas, sin tildes, sin puntos ni
    espacios sobrantes, y `&` -> `y`. Así 'UNERGY S.A.S.' == 'UNERGY S.A.S', y
    'NAOS GENERACION…' == 'NAOS GENERACIÓN…'."""
    if not nombre:
        return ""
    s = unicodedata.normalize("NFKD", str(nombre)).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " y ")
    return " ".join(s.split()).rstrip(". ").strip()


def _dec(valor) -> Decimal | None:
    """Normaliza a Decimal; devuelve None para vacío, no-numérico o cero."""
    if valor is None or valor == "":
        return None
    try:
        d = Decimal(str(valor))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return None if d == 0 else d


def _clip(lower, upper, inicio, fin) -> DateRange | None:
    """Recorta [lower, upper) a [inicio, fin). None = extremo abierto. Vacío -> None."""
    if inicio is not None:
        lower = inicio if lower is None else max(lower, inicio)
    if fin is not None:
        upper = fin if upper is None else min(upper, fin)
    if lower is not None and upper is not None and lower >= upper:
        return None
    return DateRange(lower, upper)  # bounds '[)'


def _year_range(anio: int, mes: int, dia: int) -> tuple[dt.date, dt.date]:
    """[date(anio, mes, dia), date(anio+1, mes, dia)), tolerando 29-feb."""
    def _safe(y, m, d):
        try:
            return dt.date(y, m, d)
        except ValueError:
            return dt.date(y, m, 28)  # 29-feb -> 28-feb

    return _safe(anio, mes, dia), _safe(anio + 1, mes, dia)


def _add_month(anio: int, mes: int) -> tuple[int, int]:
    return (anio + 1, 1) if mes == 12 else (anio, mes + 1)


class Command(BaseCommand):
    help = "Puebla contratos/contrato_partes/contrato_proyectos/contrato_tarifas (aditivo)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Revierte al final.")
        parser.add_argument(
            "--reset", action="store_true",
            help="Borra las 4 tablas nuevas antes de recargar (idempotencia).",
        )
        parser.add_argument("--limit", type=int, default=None, help="Máx. contratos por fuente.")
        parser.add_argument(
            "--remapear-fks", action="store_true", dest="remapear_fks",
            help=(
                "CORTE: re-apunta a contratos.id las 17 columnas FK que hoy miran a "
                "ppa_contratos/contratos_servicio. Destructivo sobre esas tablas; "
                "corre en la ventana de mantenimiento, junto con la migración 0009. "
                "Incompatible con --limit (el remapeo necesita TODOS los contratos)."
            ),
        )

    # ------------------------------------------------------------------ run
    def handle(self, *args, **opts):
        self.dry = opts["dry_run"]
        self.limit = opts["limit"]
        self.remapear = opts["remapear_fks"]
        if self.remapear and self.limit:
            self.stderr.write("--remapear-fks no admite --limit: aborta.")
            return
        self.rep = defaultdict(int)
        # Mapas viejo->nuevo id para re-apuntar las FKs de las tablas dependientes.
        self.map_ppa: dict[int, int] = {}
        self.map_srv: dict[int, int] = {}
        self.omitidas_por_cero: list[tuple] = []
        self.partes_no_resueltas: list[tuple] = []
        self.solapes: list[tuple] = []
        self.rangos_vacios: list[tuple] = []
        # Índice de clientes en memoria (pocos miles): por nit y por razón social.
        self._por_nit = {}
        self._por_nombre = {}
        for c in Cliente.objects.all().only("id", "nit_cedula", "razon_social_nombre"):
            if c.nit_cedula:
                self._por_nit[c.nit_cedula.strip()] = c
            if c.razon_social_nombre:
                self._por_nombre.setdefault(_norm(c.razon_social_nombre), c)

        try:
            with transaction.atomic():
                if opts["reset"]:
                    self._reset()
                elif Contrato.objects.exists():
                    self.stderr.write(
                        "Ya hay filas en `contratos`. Usá --reset para recargar, o "
                        "--dry-run para simular."
                    )
                    return
                self._crear_clientes_faltantes()
                self._migrar_ppa()
                self._migrar_servicio()
                if self.remapear:
                    self._remapear_fks()
                if self.dry:
                    raise _Rollback
        except _Rollback:
            self.stdout.write(self.style.WARNING("\n--dry-run: transacción revertida.\n"))

        self._reporte()

    def _reset(self):
        # CASCADE del FK cubre hijos, pero borro explícito para dejar claro el alcance.
        n = ContratoTarifa.objects.all().delete()[0]
        ContratoParte.objects.all().delete()
        ContratoProyecto.objects.all().delete()
        ContratoTipo.objects.all().delete()
        m = Contrato.objects.all().delete()[0]
        self.stdout.write(f"reset: {m} contratos y {n} tarifas borrados.")

    @staticmethod
    def _leer(tabla, extra=""):
        """Lee una tabla VIEJA por SQL crudo -> lista de SimpleNamespace (acceso por
        atributo, igual que un objeto ORM). jsonb vuelve ya parseado."""
        with connection.cursor() as cur:
            cur.execute(f'SELECT * FROM "{tabla}" {extra}')
            cols = [c[0] for c in cur.description]
            return [SimpleNamespace(**dict(zip(cols, fila))) for fila in cur.fetchall()]

    # ------------------------------------------------------------------ PPA
    def _migrar_ppa(self):
        filas = self._leer("ppa_contratos", "ORDER BY id")
        if self.limit:
            filas = filas[: self.limit]
        proyectos_por_contrato = defaultdict(list)
        for cp in self._leer("ppa_contrato_proyectos"):
            proyectos_por_contrato[cp.contrato_id].append(cp.proyecto_id)
        # Tarifas mensuales de la tabla vieja, agrupadas por contrato viejo.
        self._tarifas_ppa = defaultdict(list)
        for t in self._leer("ppa_tarifas", 'ORDER BY "año", mes'):
            self._tarifas_ppa[t.contrato_id].append(t)

        for p in filas:
            contrato = Contrato.objects.create(
                estado=EstadoContrato.FIRMADO,
                numero_codigo_contrato=p.numero_codigo_contrato,
                nombre_interno=p.nombre_interno,
                fecha_inicio=p.fecha_inicio,
                fecha_fin=p.fecha_fin,
                tarifa_base=p.tarifa_base,
                indice_indexacion=p.indice_indexacion,
                renovacion_automatica=bool(p.renovacion_automatica),
                # partes denormalizadas + FK escalares
                comprador_id=p.comprador_id,
                vendedor_id=p.vendedor_id,
                comprador_nombre=p.comprador_nombre,
                comprador_nit=p.comprador_nit,
                vendedor_nombre=p.vendedor_nombre,
                vendedor_nit=p.vendedor_nit,
                # específicas de compraventa
                responsable_id=p.responsable_id,
                tipo_contrato=p.tipo_contrato,
                periodicidad_indexacion=p.periodicidad_indexacion,
                periodo_indexacion_base=p.periodo_indexacion_base,
                valor_indexacion_base=p.valor_indexacion_base,
                cantidad_minima_kwh_mes=p.cantidad_minima_kwh_mes,
                cantidad_maxima_kwh_mes=p.cantidad_maxima_kwh_mes,
                periodicidad_facturacion=p.periodicidad_facturacion,
                tiempo_pago=p.tiempo_pago,
                condiciones_pago=p.condiciones_pago,
                gescon_codigo=p.gescon_codigo,
                gescon_fecha_inicio=p.gescon_fecha_inicio,
                gescon_fecha_fin=p.gescon_fecha_fin,
                gescon_precio=p.gescon_precio,
                gescon_cantidades_kwh=p.gescon_cantidades_kwh,
                codigo_sic=p.codigo_sic,
                es_comunidad_energetica=p.es_comunidad_energetica,
                fecha_entrada_comunidad=p.fecha_entrada_comunidad,
                nombre_comunidad=p.nombre_comunidad,
                deleted_at=p.deleted_at,
            )
            self.map_ppa[p.id] = contrato.id
            ContratoTipo.objects.create(
                contrato=contrato, tipo=TipoContrato.COMPRAVENTA_ENERGIA
            )
            self.rep["contratos_compraventa"] += 1
            self.rep[f"tipo:{TipoContrato.COMPRAVENTA_ENERGIA}"] += 1

            self._parte(contrato, p.comprador_id, p.comprador_nombre, p.comprador_nit,
                        ContratoRol.COMPRADOR, ("ppa", p.id))
            self._parte(contrato, p.vendedor_id, p.vendedor_nombre, p.vendedor_nit,
                        ContratoRol.VENDEDOR, ("ppa", p.id))

            for proyecto_id in proyectos_por_contrato.get(p.id, []):
                ContratoProyecto.objects.create(contrato=contrato, proyecto_id=proyecto_id)
                self.rep["links_proyecto"] += 1

            self._tarifas_energia(contrato, p)

    def _tarifas_energia(self, contrato, p):
        """ppa_tarifas -> concepto energia (cop_kwh), colapsando corridas contiguas de
        igual valor en una fila con vigencia por rango."""
        filas = self._tarifas_ppa.get(p.id, [])  # ya ordenadas por (año, mes)
        # Colapsa por valor igual y contiguo (mes a mes).
        runs = []  # (valor, primer(año,mes), siguiente(año,mes) tras el último)
        for t in filas:
            val = _dec(t.tarifa)
            if val is None:
                self.omitidas_por_cero.append((f"ppa:{p.id}", "energia", "0/null"))
                continue
            nxt = _add_month(t.año, t.mes)
            if runs and runs[-1][0] == val and runs[-1][2] == (t.año, t.mes):
                runs[-1] = (val, runs[-1][1], nxt)
            else:
                runs.append((val, (t.año, t.mes), nxt))

        candidatos = []
        for i, (val, (y0, m0), (y1, m1)) in enumerate(runs):
            rango = _clip(dt.date(y0, m0, 1), dt.date(y1, m1, 1), p.fecha_inicio, p.fecha_fin)
            if rango is None:
                self.rangos_vacios.append((f"ppa:{p.id}", "energia", f"{y0}-{m0}"))
                continue
            if i == 0:
                candidatos.append(dict(concepto=TarifaConcepto.ENERGIA, valor=val,
                                       unidad=TarifaUnidad.COP_KWH, vigencia=rango,
                                       origen=TarifaOrigen.PACTADA))
            else:
                candidatos.append(dict(concepto=TarifaConcepto.ENERGIA, valor=val,
                                       unidad=TarifaUnidad.COP_KWH, vigencia=rango,
                                       origen=TarifaOrigen.INDEXACION, indice="IPP"))
        self._insertar_tarifas(contrato, f"ppa:{p.id}", candidatos)

    # -------------------------------------------------------------- servicio
    def _tipos_servicio(self, c) -> set:
        """Conjunto de tipos de un contrato de servicio. representación/CGM comparten
        fila: los tipos salen de qué tarifas están cargadas (admin cuenta como
        representación)."""
        sa = c.servicio_aplica
        if sa in ("representacion", "cgm"):
            tipos = set()
            if _dec(c.tarifa_representacion) is not None:
                tipos.add(TipoContrato.REPRESENTACION)
            if _dec(c.tarifa_cgm) is not None:
                tipos.add(TipoContrato.CGM)
            if _dec(c.tarifa_admin) is not None:
                tipos.add(TipoContrato.REPRESENTACION)
            return tipos or {TipoContrato.REPRESENTACION}
        return set(TIPOS_POR_SERVICIO.get(sa, set()))

    def _migrar_servicio(self):
        filas = self._leer("contratos_servicio", "ORDER BY id")
        if self.limit:
            filas = filas[: self.limit]
        for c in filas:
            tipos = self._tipos_servicio(c)
            if not tipos:
                self.rep["servicio_tipo_desconocido"] += 1
                continue

            contrato = Contrato.objects.create(
                # `estado` se guarda TAL CUAL (firmado/en_renovacion/terminado): la
                # fachada ContratoServicio lo lee sin traducir, así que los lectores que
                # comparan `== "firmado"` siguen funcionando.
                estado=c.estado or EstadoContrato.FIRMADO,
                numero_contrato=c.numero_contrato,
                nombre_interno=c.nombre_proyecto_ref,
                fecha_firma_contrato=c.fecha_firma_contrato,
                fecha_inicio=c.fecha_inicio,
                fecha_fin=c.fecha_fin,
                periodicidad_pago=c.periodicidad_pago,
                indice_indexacion=c.indice_indexacion,
                renovacion_automatica=bool(c.renovacion_automatica),
                servicio_aplica=c.servicio_aplica,
                # partes denormalizadas + FK escalares
                contratante_id=c.contratante_id,
                prestador_id=c.prestador_id,
                inversionista_id=c.inversionista_id,
                proyecto_id=c.proyecto_id,
                contratante_nombre=c.contratante_nombre,
                contratante_nit=c.contratante_nit,
                prestador_nombre=c.prestador_nombre,
                prestador_nit=c.prestador_nit,
                inversionista_nombre=c.inversionista_nombre,
                # tarifas escalares e indexación JSON (redundantes, autoritativas hoy)
                tarifa_admin=c.tarifa_admin,
                tarifa_cgm=c.tarifa_cgm,
                tarifa_representacion=c.tarifa_representacion,
                tarifa_mensual=c.tarifa_mensual,
                indexacion_anual=c.indexacion_anual,
                indexacion_mensual=c.indexacion_mensual,
                indexacion_cgm=c.indexacion_cgm,
                indexacion_representacion=c.indexacion_representacion,
                # específicas de servicio
                cgm_codigo_sic=c.cgm_codigo_sic,
                fecha_inicio_om=c.fecha_inicio_om,
                fecha_indexacion=c.fecha_indexacion,
                responsable_iva=bool(c.responsable_iva),
                estado_pago=c.estado_pago,
                portafolio=c.portafolio,
                codigo_sun_factory=c.codigo_sun_factory,
                nombre_proyecto_ref=c.nombre_proyecto_ref,
                ubicacion_lat=c.ubicacion_lat,
                ubicacion_lng=c.ubicacion_lng,
                plan_datos_gb=c.plan_datos_gb,
                velocidad_mbps=c.velocidad_mbps,
                tipo_conexion=c.tipo_conexion,
                linea_servicio=c.linea_servicio,
                id_router=c.id_router,
                numero_kit=c.numero_kit,
                latencia_ms=c.latencia_ms,
                wifi_seguridad=c.wifi_seguridad,
                wifi_password=c.wifi_password,
            )
            self.map_srv[c.id] = contrato.id
            for t in sorted(tipos):
                ContratoTipo.objects.create(contrato=contrato, tipo=t)
                self.rep[f"tipo:{t}"] += 1
            self.rep["contratos_servicio"] += 1

            self._partes_servicio(contrato, c)
            if c.proyecto_id:
                ContratoProyecto.objects.create(contrato=contrato, proyecto_id=c.proyecto_id)
                self.rep["links_proyecto"] += 1

            if c.servicio_aplica in ("representacion", "cgm"):
                self._tarifas_representacion(contrato, c)
            else:
                self._tarifas_canon(contrato, c)

    def _partes_servicio(self, contrato, c):
        # contratante es siempre el dueño/contraparte; el prestador cambia de rol por servicio.
        self._parte(contrato, c.contratante_id, c.contratante_nombre, c.contratante_nit,
                    ContratoRol.PROPIETARIO, ("srv", c.id))
        if c.inversionista_id:
            self._parte(contrato, c.inversionista_id, c.inversionista_nombre, None,
                        ContratoRol.PROPIETARIO, ("srv", c.id))
        rol_prestador = {
            "representacion": ContratoRol.REPRESENTANTE,
            "cgm": ContratoRol.REPRESENTANTE,
            "mantenimiento": ContratoRol.MANTENEDOR,
            "arriendo": ContratoRol.ARRENDADOR,
            "internet": ContratoRol.OPERADOR,
        }.get(c.servicio_aplica, ContratoRol.OPERADOR)
        self._parte(contrato, c.prestador_id, c.prestador_nombre, c.prestador_nit,
                    rol_prestador, ("srv", c.id))

    def _tarifas_representacion(self, contrato, c):
        candidatos = []
        candidatos += self._serie(c, "indexacion_cgm", _dec(c.tarifa_cgm),
                                  TarifaConcepto.CGM, TarifaUnidad.COP_KWH)
        candidatos += self._serie(c, "indexacion_representacion", _dec(c.tarifa_representacion),
                                  TarifaConcepto.REPRESENTACION, TarifaUnidad.COP_KWH)
        # Administración: porcentaje, sin serie histórica -> fila migracion abierta.
        admin = _dec(c.tarifa_admin)
        if admin is not None:
            rango = _clip(None, None, c.fecha_inicio, c.fecha_fin)
            if rango is not None:
                candidatos.append(dict(
                    concepto=TarifaConcepto.ADMINISTRACION, valor=admin,
                    unidad=TarifaUnidad.PORCENTAJE, vigencia=rango,
                    origen=TarifaOrigen.MIGRACION,
                    nota=("Migrado del escalar contratos_servicio.tarifa_admin; la "
                          "administración se renegocia y no tiene serie histórica (D-24 §b): "
                          "vigencia inicial no confirmada."),
                ))
        else:
            self.omitidas_por_cero.append((f"srv:{c.id}", "administracion", "0/null"))
        self._insertar_tarifas(contrato, f"srv:{c.id}", candidatos)

    def _tarifas_canon(self, contrato, c):
        base = _dec(c.tarifa_mensual) or _dec(c.tarifa_base)
        unidad = TarifaUnidad.COP_MES if _dec(c.tarifa_mensual) else TarifaUnidad.COP_TOTAL
        # La serie del canon vive en indexacion_anual/mensual.
        serie_json = c.indexacion_mensual or c.indexacion_anual
        candidatos = self._serie_desde_lista(
            c, serie_json, base, TarifaConcepto.CANON, unidad
        )
        self._insertar_tarifas(contrato, f"srv:{c.id}", candidatos)

    # --------------------------------------------------------------- series
    def _serie(self, c, campo_json, escalar, concepto, unidad):
        return self._serie_desde_lista(c, getattr(c, campo_json, None), escalar, concepto, unidad)

    def _serie_desde_lista(self, c, lista, escalar, concepto, unidad):
        """Convierte una lista JSONB [{año|anio, valor, ipc, esBase|es_base}] en filas.
        Si no hay serie, cae al escalar como fila `migracion` abierta."""
        mes = (c.fecha_firma_contrato or c.fecha_inicio or dt.date(2000, 1, 1)).month
        dia = (c.fecha_firma_contrato or c.fecha_inicio or dt.date(2000, 1, 1)).day
        candidatos = []
        if isinstance(lista, list) and lista:
            for e in lista:
                if not isinstance(e, dict):
                    continue
                anio = e.get("año", e.get("anio"))
                val = _dec(e.get("valor"))
                if anio is None or val is None:
                    if val is None:
                        self.omitidas_por_cero.append((f"srv:{c.id}", concepto, "0/null"))
                    continue
                lo, hi = _year_range(int(anio), mes, dia)
                rango = _clip(lo, hi, c.fecha_inicio, c.fecha_fin)
                if rango is None:
                    self.rangos_vacios.append((f"srv:{c.id}", concepto, str(anio)))
                    continue
                es_base = bool(e.get("esBase", e.get("es_base")))
                ipc = _dec(e.get("ipc"))
                if es_base:
                    candidatos.append(dict(concepto=concepto, valor=val, unidad=unidad,
                                           vigencia=rango, origen=TarifaOrigen.PACTADA))
                else:
                    candidatos.append(dict(
                        concepto=concepto, valor=val, unidad=unidad, vigencia=rango,
                        origen=TarifaOrigen.INDEXACION,
                        indice=(c.indice_indexacion or "IPC"), indice_pct=ipc))
            return candidatos

        # Sin serie: escalar como fila migracion abierta.
        if escalar is not None:
            rango = _clip(None, None, c.fecha_inicio, c.fecha_fin)
            if rango is not None:
                candidatos.append(dict(
                    concepto=concepto, valor=escalar, unidad=unidad, vigencia=rango,
                    origen=TarifaOrigen.MIGRACION,
                    nota=(f"Migrado del escalar contratos_servicio ({concepto}); "
                          "sin serie de indexación: vigencia inicial no confirmada (D-24)."),
                ))
        else:
            self.omitidas_por_cero.append((f"srv:{c.id}", concepto, "0/null"))
        return candidatos

    # ----------------------------------------------------------- inserción
    def _insertar_tarifas(self, contrato, origen_id, candidatos):
        """Detecta solapes por concepto ANTES de insertar (el EXCLUDE los rechazaría)."""
        por_concepto = defaultdict(list)
        for cand in candidatos:
            por_concepto[cand["concepto"]].append(cand)
        for concepto, filas in por_concepto.items():
            filas.sort(key=lambda x: (x["vigencia"].lower or dt.date.min))
            solapa = False
            for a, b in zip(filas, filas[1:]):
                if self._solapan(a["vigencia"], b["vigencia"]):
                    solapa = True
                    break
            if solapa:
                self.solapes.append((origen_id, concepto, len(filas)))
                continue  # no inserto ese concepto: es dato a corregir
            for cand in filas:
                ContratoTarifa.objects.create(contrato=contrato, **cand)
                self.rep[f"tarifa:{concepto}:{cand['origen']}"] += 1

    @staticmethod
    def _solapan(r1: DateRange, r2: DateRange) -> bool:
        lo1, hi1, lo2, hi2 = r1.lower, r1.upper, r2.lower, r2.upper
        izq = lo1 if (lo2 is None or (lo1 is not None and lo1 >= lo2)) else lo2
        der = hi1 if (hi2 is None or (hi1 is not None and hi1 <= hi2)) else hi2
        if izq is None or der is None:
            return True  # algún extremo abierto en ambos lados -> se tocan
        return izq < der

    # ------------------------------------------------------------- remapeo
    def _remapear_fks(self):
        """Re-apunta a contratos.id las columnas FK que hoy miran a las tablas viejas.

        Las FK todavía referencian ppa_contratos/contratos_servicio, así que se
        desactivan los triggers de FK con `SET LOCAL session_replication_role =
        'replica'` (scope de transacción) para poder poner ids de `contratos`. La
        migración 0009 cambia después el destino de la constraint y valida contra
        estos valores ya remapeados, y elimina las tablas viejas.

        Requiere un rol con permiso para `session_replication_role` (superuser en la
        base gestionada). Si falla por permiso, hay que hacer el remapeo dentro de la
        0009 con las constraints ya soltadas."""
        with connection.cursor() as cur:
            cur.execute("SET LOCAL session_replication_role = 'replica'")
            self._cargar_map(cur, "_map_ppa", self.map_ppa)
            self._cargar_map(cur, "_map_srv", self.map_srv)
            for tabla, col in REMAP_PPA:
                self.rep[f"remap:{tabla}.{col}"] = self._remap(cur, tabla, col, "_map_ppa")
            for tabla, col in REMAP_SRV:
                self.rep[f"remap:{tabla}.{col}"] = self._remap(cur, tabla, col, "_map_srv")
            cur.execute("SET LOCAL session_replication_role = 'origin'")

    @staticmethod
    def _cargar_map(cur, nombre, mapa):
        cur.execute(f'DROP TABLE IF EXISTS "{nombre}"')
        cur.execute(f'CREATE TEMP TABLE "{nombre}" (old_id bigint PRIMARY KEY, new_id bigint)')
        if mapa:
            cur.executemany(
                f'INSERT INTO "{nombre}" (old_id, new_id) VALUES (%s, %s)',
                list(mapa.items()),
            )

    @staticmethod
    def _remap(cur, tabla, col, mapa_tmp) -> int:
        # DOS PASOS por un espacio de ids DISJUNTO (negativo). Los ids nuevos de
        # `contratos` solapan el rango de los viejos, así que un UPDATE directo
        # (que permuta ids dentro del mismo espacio) rompe las UNIQUE que incluyen
        # la FK (p. ej. ppa_tarifas(contrato_id, año, mes)) con una colisión
        # transitoria. Mapear primero a -new_id (negativos, disjuntos de los
        # positivos viejos) y luego voltear a +new_id evita el choque.
        cur.execute(
            f'UPDATE "{tabla}" AS t SET "{col}" = -m.new_id '
            f'FROM "{mapa_tmp}" AS m WHERE t."{col}" = m.old_id'
        )
        n = cur.rowcount
        cur.execute(f'UPDATE "{tabla}" SET "{col}" = -"{col}" WHERE "{col}" < 0')
        return n

    # ------------------------------------------------------------- partes
    def _parte(self, contrato, cliente_id, nombre, nit, rol, origen):
        cliente = self._resolver_cliente(cliente_id, nombre, nit)
        if cliente is None:
            if cliente_id or nombre or nit:
                self.partes_no_resueltas.append((origen, rol, nombre or nit or cliente_id))
            return
        _, creada = ContratoParte.objects.get_or_create(
            contrato=contrato, cliente=cliente, rol=rol
        )
        if creada:
            self.rep["partes"] += 1

    def _resolver_cliente(self, cliente_id, nombre, nit):
        if cliente_id:
            c = Cliente.objects.filter(id=cliente_id).first()
            if c is not None:
                return c
        if nit and nit.strip() in self._por_nit:
            return self._por_nit[nit.strip()]
        clave = _norm(nombre)
        # Alias para clientes que existen con otro formato de nombre.
        clave = ALIAS_CLIENTE.get(clave, clave)
        return self._por_nombre.get(clave)

    def _crear_clientes_faltantes(self):
        """Crea los clientes que existen en los contratos pero no en `clientes`
        (get_or_create por razón social), y los agrega al índice de nombres."""
        for nombre, nit in CLIENTES_A_CREAR:
            cliente, creado = Cliente.objects.get_or_create(
                razon_social_nombre=nombre, defaults={"nit_cedula": nit}
            )
            self._por_nombre.setdefault(_norm(nombre), cliente)
            if creado:
                self.rep["clientes_creados"] += 1

    # ------------------------------------------------------------- reporte
    def _reporte(self):
        self.stdout.write(self.style.MIGRATE_HEADING("\n== Backfill contratos unificados =="))
        for k in sorted(self.rep):
            self.stdout.write(f"  {k}: {self.rep[k]}")
        self.stdout.write(f"\n  omitidas_por_cero: {len(self.omitidas_por_cero)}")
        for o in self.omitidas_por_cero:
            self.stdout.write(f"    - {o}")
        self.stdout.write(f"  partes_no_resueltas: {len(self.partes_no_resueltas)}")
        for o in self.partes_no_resueltas:
            self.stdout.write(f"    - {o}")
        self.stdout.write(f"  rangos_vacios (saltados): {len(self.rangos_vacios)}")
        for o in self.rangos_vacios:
            self.stdout.write(f"    - {o}")
        self.stdout.write(f"  solapes (concepto NO insertado, dato a corregir): {len(self.solapes)}")
        for o in self.solapes:
            self.stdout.write(f"    - {o}")
        self.stdout.write("")
