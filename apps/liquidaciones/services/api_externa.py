"""Cliente de la API de Liquidaciones de Unergy (api.unergy.io).

Los datos maestros que usa el ciclo de liquidaciones -- códigos SIC/FRT,
``ac_power`` y los flags de generador/comercializador -- viven en esa API y no
en esta base de datos. Este módulo es el único punto que habla HTTP con ella.

Particularidades de la API, verificadas contra producción:

* La autenticación es ``POST /api/accounts/<account_id>/`` con login+password y
  devuelve un JWT de acceso. Un 401 posterior significa token vencido: hay que
  renovarlo y reintentar, no es "sin datos".
* Rechaza clientes sin un ``User-Agent`` conocido, así que se envía uno fijo.
* ``/api/admin/*`` exige ``is_staff`` y ``/api/liquidaciones/*`` pertenecer al
  grupo ``admin``; la cuenta de servicio debe cumplir ambos.
"""
import datetime
import logging
import threading
import time
from decimal import Decimal
from enum import Enum
from typing import Any

import httpx

import os


class _Config:
    """Lee la configuración del entorno, como los demás servicios portados.

    Se conserva el acceso por atributo (`settings.X`) para no tocar las diez
    líneas que la usan: el cliente vino tal cual desde
    `app/services/liquidaciones_api.py` y cuanto menos se le cambie, menos hay
    que volver a verificar contra la API externa.
    """

    def __getattr__(self, nombre: str) -> str:
        # Delegado en `apps.comun.config`, que es donde viven los defaults que
        # traía pydantic y que el `.env` nunca necesitó declarar. Leer
        # `os.environ` a secas devolvía "" para `UNERGY_API_URL` y armaba una
        # URL relativa contra la que ninguna petición podía salir.
        from apps.comun.config import settings as compartido

        return getattr(compartido, nombre)


settings = _Config()

logger = logging.getLogger("liquidaciones_api")

# Rutas de la API externa (sin hardcodearlas en los endpoints que las consumen).
PATH_LOGIN = "/api/accounts/{account_id}/"
# Proyectos: se migró de `/api/admin/project/` (que además exigía `is_staff`) a
# la ruta de liquidaciones, que basta con el grupo `admin` y sí expone los
# subproyectos con sus ids de Quoia.
PATH_PROYECTOS = "/api/liquidaciones/projects/"
PATH_PROYECTO = "/api/liquidaciones/projects/{topico}/"
PATH_SUBPROYECTOS = "/api/liquidaciones/subprojects/"
PATH_SUBPROYECTO = "/api/liquidaciones/subprojects/{topico}/"
PATH_TAREA = "/api/liquidaciones/task_status/{task_id}/"
PATH_FACTURAS_XM = "/api/liquidaciones/xm-invoices/"

# Históricos, solo lectura (5.3). Sirven para auditar los insumos y los
# resultados intermedios del ciclo sin entrar al Django admin.
PATH_IPP_HISTORICO = "/api/liquidaciones/monthly_ipps/"
PATH_LIQUIDACIONES_MERCADO = "/api/liquidaciones/market_settlements/"
PATH_CONTRATOS_DESPACHADOS = "/api/liquidaciones/disp_contracts_ftp_xm/"

# Datos maestros (3 de la guia).
PATH_CONTRATOS = "/api/liquidaciones/contract_energies/"
# Detalle de los tres recursos del contrato. `OPTIONS` sobre ellos responde
# `GET, PUT, PATCH`: se pueden editar. Ninguno expone DELETE, así que no hay
# forma de desvincular un proyecto ni de borrar un contrato.
PATH_CONTRATO = "/api/liquidaciones/contract_energies/{id}/"
PATH_CONTRATO_PROYECTO = "/api/liquidaciones/contract_energy_projects/{id}/"
PATH_CANTIDAD = "/api/liquidaciones/energy_contract_quantities/{id}/"
PATH_CONTRATO_PROYECTOS = "/api/liquidaciones/contract_energy_projects/"
PATH_CANTIDADES = "/api/liquidaciones/energy_contract_quantities/"
PATH_COSTOS = "/api/liquidaciones/revenue_and_costs/"
PATH_COSTOS_XLSX = "/api/liquidaciones/create_revenue_and_cost_xlsx/"

# Catalogos, solo lectura (3.7).
PATH_TIPOS_COSTO = "/api/liquidaciones/revenue_and_cost_types/"
PATH_EMPRESAS = "/api/liquidaciones/companies/"
PATH_PRECIOS_ENERGIA = "/api/liquidaciones/energy_prices/"

# Ciclo mensual (4). OJO: los tres sin slash final dan 404 si se les agrega.
PATH_IPP = "/api/liquidaciones/fetch_monthly_ipp"
PATH_FTP = "/api/liquidaciones/fetch_data_from_xm"
PATH_REPARTIR = "/api/liquidaciones/set_xm_variables_from_processed_invoices"
PATH_LIQUIDAR = "/api/liquidaciones/calculate_project_market_settlement/"
PATH_ESTADO_RESULTADOS_JSON = "/api/liquidaciones/income_statement_data/"
PATH_ESTADO_RESULTADOS_XLSX = "/api/liquidaciones/get_income_statement/"
PATH_CRUCE_FACTURAS = "/api/liquidaciones/cross_invoice_report/"
PATH_DIAGNOSTICO = "/api/liquidaciones/check_income_statement/"


class VersionLiquidacion(str, Enum):
    """Versión del ciclo: ``txf`` es la liquidación inicial, ``tx3``..``tx8`` las
    reliquidaciones que publica XM."""

    TXF = "txf"
    TX3 = "tx3"
    TX4 = "tx4"
    TX5 = "tx5"
    TX6 = "tx6"
    TX7 = "tx7"
    TX8 = "tx8"


class GrupoCosto(str, Enum):
    """Grupo al que pertenece un tipo de costo o ingreso fijo.

    ``XM`` son los conceptos de comercialización que produce el reparto de las
    facturas de XM (energía en bolsa, FAZNI, cargo por confiabilidad, servicios
    de despacho y administración, IVA, arranque y parada). Los demás son costos
    propios del proyecto y no salen en la vista de comercialización.
    """

    XM = "xm"
    OPEX = "opex"
    INTERESTS = "interests"
    DISCOUNTS = "discounts"
    ADJUSTMENT = "adjustment"


class EstadoTarea(str, Enum):
    """Estado normalizado de una tarea asíncrona.

    La API cruda tiene seis estados de Celery y, además, tareas que atrapan sus
    errores y responden ``SUCCESS`` con ``result.success = False``. Aquí se
    colapsa todo a tres, para que quien consuma no tenga que recordar esa trampa.
    """

    EN_CURSO = "en_curso"
    EXITO = "exito"
    FALLO = "fallo"


# Estados de Celery que ya no van a cambiar.
_ESTADOS_TERMINALES = {"SUCCESS", "FAILURE", "REVOKED"}

# Campos de configuración del proyecto que expone esta integración (§3.1 de la
# guía). Se listan explícitamente porque el recurso trae ~65 campos y solo estos
# intervienen en el ciclo de liquidaciones.
CAMPOS_PROYECTO = (
    "sic_gen",
    "sic_con",
    "frt_gen",
    "frt_con",
    "ac_power",
    "from_generator",
    "from_commercializer",
)

# Ids de Quoia. No son campos del proyecto: viven en cada subproyecto (§3.1).
# Los dos de reporte admiten máximo 4 caracteres; el del nodo, 50.
CAMPOS_QUOIA = (
    "quoia_report_gen_id",
    "quoia_report_con_id",
    "quoia_node_id",
)
MAX_LARGO_REPORTE_QUOIA = 4
MAX_LARGO_NODO_QUOIA = 50

_TIMEOUT = httpx.Timeout(15.0, read=60.0)
_USER_AGENT = "PostmanRuntime/7.50.0"

_token: str | None = None
_token_lock = threading.Lock()


class LiquidacionesAPIError(RuntimeError):
    """Falla al comunicarse con la API de Liquidaciones."""


def _credenciales() -> tuple[str, str]:
    """Credenciales de la cuenta de servicio, con respaldo en las de UNERGY_*."""
    login = settings.LIQUIDACIONES_LOGIN or settings.UNERGY_LOGIN
    password = settings.LIQUIDACIONES_PASSWORD or settings.UNERGY_PASSWORD
    if not (login and password and settings.UNERGY_ACCOUNT_ID):
        raise LiquidacionesAPIError(
            "Faltan credenciales: configura LIQUIDACIONES_LOGIN, "
            "LIQUIDACIONES_PASSWORD y UNERGY_ACCOUNT_ID."
        )
    return login, password


def _json_seguro(valor: Any) -> Any:
    """Convierte a tipos que `json.dumps` entienda, sin `default=`.

    httpx serializa el `json=` con `json.dumps` pelado, así que un tipo que no
    conozca lanza `TypeError` — y ese error **no** es un `httpx.HTTPError`, así
    que se escapa de los `except` de `_request` y termina en un 500 mudo, sin
    haber llegado a salir a la red.

    Es fácil toparse con eso sin darse cuenta: DRF entrega `validated_data` con
    tipos de Python, no con texto. Un `DateField` da `datetime.date` y un
    `DecimalField` da `Decimal`. Paso obligado antes de mandar cualquier cuerpo.
    """
    if isinstance(valor, dict):
        return {k: _json_seguro(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_json_seguro(v) for v in valor]
    if isinstance(valor, (datetime.datetime, datetime.date, datetime.time)):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        return float(valor)
    return valor


def _url(path: str) -> str:
    return f"{settings.UNERGY_API_URL.rstrip('/')}{path}"


def _login() -> str:
    """Pide un token nuevo y lo deja cacheado en el módulo."""
    global _token
    login, password = _credenciales()
    url = _url(PATH_LOGIN.format(account_id=settings.UNERGY_ACCOUNT_ID))
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(
                url,
                json={"login": login, "password": password},
                headers={"User-Agent": _USER_AGENT},
            )
            resp.raise_for_status()
            token = resp.json()["access"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.exception("No se pudo autenticar contra la API de Liquidaciones")
        raise LiquidacionesAPIError("No se pudo autenticar contra la API de Liquidaciones") from exc

    _token = token
    return token


def _request(method: str, path: str, **kwargs: Any) -> Any:
    """Ejecuta la llamada renovando el token una sola vez si expiró (401)."""
    global _token

    with _token_lock:
        token = _token or _login()

    if "json" in kwargs:
        kwargs = {**kwargs, "json": _json_seguro(kwargs["json"])}

    def _enviar(bearer: str) -> httpx.Response:
        with httpx.Client(timeout=_TIMEOUT) as client:
            return client.request(
                method,
                _url(path),
                headers={"Authorization": f"Bearer {bearer}", "User-Agent": _USER_AGENT},
                **kwargs,
            )

    try:
        resp = _enviar(token)
        if resp.status_code == 401:
            with _token_lock:
                token = _login()
            resp = _enviar(token)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "API de Liquidaciones respondió %s en %s", exc.response.status_code, path
        )
        raise LiquidacionesAPIError(
            f"La API de Liquidaciones respondió {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        logger.exception("Error de red hablando con la API de Liquidaciones")
        raise LiquidacionesAPIError("No se pudo contactar la API de Liquidaciones") from exc

    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise LiquidacionesAPIError("La API de Liquidaciones devolvió una respuesta no JSON") from exc


def _proyectar(proyecto: dict[str, Any]) -> dict[str, Any]:
    """Deja solo el identificador y los campos del ciclo de liquidaciones."""
    datos = {campo: proyecto.get(campo) for campo in CAMPOS_PROYECTO}
    datos["nombre_topico"] = proyecto.get("nombre_topico")
    datos["nombre_proyecto"] = proyecto.get("nombre_proyecto")
    # Los tres ids de Quoia no son del proyecto: dos son de cada subproyecto y
    # el del nodo vive en su medidor de generación de prioridad 1. La API ya los
    # devuelve resueltos aquí, así que no hay que consultarlos aparte.
    datos["subproyectos"] = [
        {campo: s.get(campo) for campo in ("topic", "name", *CAMPOS_QUOIA)}
        for s in (proyecto.get("subprojects") or [])
    ]
    return datos


def listar_proyectos() -> list[dict[str, Any]]:
    """Configuración de liquidaciones de todos los proyectos."""
    return [_proyectar(p) for p in _listar(PATH_PROYECTOS)]


def totales_ac_power() -> dict[str, Any]:
    """Suma el AC Power de los proyectos que reciben cada grupo de conceptos.

    Se calcula sobre TODOS los proyectos de la API, no sobre los que cruzan con
    esta base: es el divisor de la prorrata del reparto, y dejar uno afuera le
    sube el costo a todos los demás.
    """
    proyectos = listar_proyectos()

    def _resumen(campo: str) -> dict[str, Any]:
        marcados = [p for p in proyectos if p.get(campo) is True]
        return {
            "proyectos": len(marcados),
            "ac_power": round(sum(p.get("ac_power") or 0 for p in marcados), 4),
            "sin_ac_power": sum(1 for p in marcados if not p.get("ac_power")),
        }

    return {
        "generador": _resumen("from_generator"),
        "comercializador": _resumen("from_commercializer"),
        # Solo los que reciben algún grupo de conceptos: son los que entran al
        # divisor de la prorrata, y por tanto los únicos cuya falta de cruce
        # cambia el reparto. Los demás sin cruce son ruido.
        "topicos": [
            p["nombre_topico"] for p in proyectos
            if p.get("nombre_topico")
            and (p.get("from_generator") is True or p.get("from_commercializer") is True)
        ],
    }


def obtener_proyecto(topico: str) -> dict[str, Any]:
    """Configuración de liquidaciones de un solo proyecto, por su tópico."""
    data = _request("GET", PATH_PROYECTO.format(topico=topico))
    return _proyectar(data) if isinstance(data, dict) else {}


def actualizar_proyecto(topico: str, cambios: dict[str, Any]) -> dict[str, Any]:
    """Actualiza los campos de §3.1 de un proyecto, identificado por su tópico."""
    permitidos = {k: v for k, v in cambios.items() if k in CAMPOS_PROYECTO}
    if not permitidos:
        raise LiquidacionesAPIError("No hay campos válidos para actualizar")
    data = _request("PATCH", PATH_PROYECTO.format(topico=topico), json=permitidos)
    return _proyectar(data) if isinstance(data, dict) else {}


# ── Subproyectos e ids de Quoia ──────────────────────────────────────────────

def listar_subproyectos(**filtros: Any) -> list[dict[str, Any]]:
    """Subproyectos con sus tres ids de Quoia. Filtros: ``project``, ``topic``."""
    return _listar(PATH_SUBPROYECTOS, **filtros)


def actualizar_subproyecto(topico: str, cambios: dict[str, Any]) -> dict[str, Any]:
    """Escribe los ids de Quoia de un subproyecto.

    Es un PATCH parcial de verdad: lo que no se manda no se toca, y mandar
    ``None`` **borra** el id. Por eso quien llame debe filtrar por
    ``exclude_unset`` y no por "campo vacío".

    El ``quoia_node_id`` se guarda en el medidor de generación de prioridad 1
    del subproyecto; si ese medidor no existe, la API lo crea.
    """
    permitidos = {k: v for k, v in cambios.items() if k in CAMPOS_QUOIA}
    if not permitidos:
        raise LiquidacionesAPIError("No hay ids de Quoia para actualizar")

    for campo in ("quoia_report_gen_id", "quoia_report_con_id"):
        valor = permitidos.get(campo)
        if valor is not None and len(str(valor)) > MAX_LARGO_REPORTE_QUOIA:
            raise LiquidacionesAPIError(
                f"«{campo}» admite máximo {MAX_LARGO_REPORTE_QUOIA} caracteres"
            )
    nodo = permitidos.get("quoia_node_id")
    if nodo is not None and len(str(nodo)) > MAX_LARGO_NODO_QUOIA:
        raise LiquidacionesAPIError(
            f"«quoia_node_id» admite máximo {MAX_LARGO_NODO_QUOIA} caracteres"
        )

    data = _request("PATCH", PATH_SUBPROYECTO.format(topico=topico), json=permitidos)
    return data if isinstance(data, dict) else {}


# ── Históricos del ciclo (§5.3) ──────────────────────────────────────────────

def listar_ipp_historico(**filtros: Any) -> list[dict[str, Any]]:
    """IPP del DANE ya consultados, de más reciente a más antiguo.

    Puede traer más de una fila por mes: el registro se guarda con la fecha en
    que se consultó al DANE, no con el día 1. Para un valor único por mes hay
    que quedarse con el de ``date`` más reciente -- eso lo hace :func:`ipp_del_mes`.
    """
    return _listar(PATH_IPP_HISTORICO, **filtros)


def ipp_del_mes(year: int, month: int) -> dict[str, Any] | None:
    """El IPP vigente de un mes: el consultado más recientemente."""
    filas = listar_ipp_historico(year=year, month=month)
    if not filas:
        return None
    return max(filas, key=lambda f: str(f.get("date") or ""))


def listar_liquidaciones_mercado(**filtros: Any) -> list[dict[str, Any]]:
    """Despachos ya liquidados (§4.5), por día y contrato.

    Filtros: ``year``, ``month``, ``version``, ``data_type``, ``project``
    (nombre_topico) y ``contract_energy_project`` (id).
    """
    return _listar(PATH_LIQUIDACIONES_MERCADO, **filtros)


def listar_contratos_despachados(**filtros: Any) -> list[dict[str, Any]]:
    """Energía **contratada** por hora que trae el FTP de XM (§4.2).

    ``con_hour01``..``con_hour24`` son las 24 horas en kWh. Las columnas de
    energía despachada (``disp_hour*``) no se exponen en este endpoint.

    Filtros: ``year``, ``month``, ``date`` (día exacto), ``project``, ``version``.
    """
    return _listar(PATH_CONTRATOS_DESPACHADOS, **filtros)


# ── Tareas asíncronas ────────────────────────────────────────────────────────
# Varios endpoints del ciclo (FTP de XM, liquidar, repartir, ER, cruce) devuelven
# un ``task_id`` en vez del resultado. Consultarlas tiene dos trampas y las dos
# se resuelven aquí, en un solo lugar:
#
#   1. ``status: "SUCCESS"`` NO significa que salió bien. Varias tareas atrapan
#      sus errores y devuelven ``result.success = False`` con estado SUCCESS.
#   2. Un ``task_id`` inexistente responde ``PENDING``, no 404. No hay forma de
#      distinguir "en cola" de "no existe", así que quien espere una tarea tiene
#      que ponerle su propio límite de tiempo.

def _mensaje_de_tarea(resultado: Any, estado: EstadoTarea, estado_crudo: str) -> str:
    """Frase corta para mostrarle al usuario, sin filtrar el error crudo del proveedor."""
    if isinstance(resultado, dict):
        mensaje = resultado.get("message") or resultado.get("error")
        if not mensaje and resultado.get("errors"):
            mensaje = str(resultado["errors"])
        if mensaje:
            return str(mensaje)
    if estado is EstadoTarea.EXITO:
        return "La tarea terminó correctamente."
    if estado is EstadoTarea.FALLO:
        return f"La tarea terminó en estado {estado_crudo}."
    return "La tarea sigue en proceso."


def consultar_tarea(task_id: str) -> dict[str, Any]:
    """Estado normalizado de una tarea asíncrona.

    Devuelve ``estado`` (ver :class:`EstadoTarea`), ``terminada`` y el ``resultado``
    crudo por si el consumidor necesita algo puntual de adentro (una ``drive_url``,
    por ejemplo).
    """
    data = _request("GET", PATH_TAREA.format(task_id=task_id))
    if not isinstance(data, dict):
        raise LiquidacionesAPIError("La API de Liquidaciones devolvió un estado de tarea inesperado")

    estado_crudo = str(data.get("status") or "")
    resultado = data.get("result")

    if estado_crudo not in _ESTADOS_TERMINALES:
        estado = EstadoTarea.EN_CURSO
    elif estado_crudo != "SUCCESS":
        estado = EstadoTarea.FALLO
    # SUCCESS no implica que salió bien: manda `result.success`.
    elif isinstance(resultado, dict) and resultado.get("success") is False:
        estado = EstadoTarea.FALLO
    else:
        estado = EstadoTarea.EXITO

    return {
        "task_id": task_id,
        # .value explícito: `str(EstadoTarea.EXITO)` da "EstadoTarea.EXITO", no "exito".
        "estado": estado.value,
        "estado_crudo": estado_crudo,
        "terminada": estado is not EstadoTarea.EN_CURSO,
        "mensaje": _mensaje_de_tarea(resultado, estado, estado_crudo),
        "resultado": resultado if isinstance(resultado, dict) else None,
    }


# ── Facturas de XM ───────────────────────────────────────────────────────────
# Límites que impone la API al subir (§4.3 de la guía).
MAX_FACTURAS_POR_LOTE = 20
MAX_BYTES_POR_FACTURA = 10 * 1024 * 1024


def listar_facturas_xm(**filtros: Any) -> dict[str, Any]:
    """Facturas de XM de un período con su bloque ``readiness``.

    ``readiness.ready_for_distribution`` es la precondición de repartir (§4.6):
    si viene en ``false``, ``blockers`` dice exactamente qué falta.
    """
    params = {k: v for k, v in filtros.items() if v not in (None, "")}
    data = _request("GET", PATH_FACTURAS_XM, params=params)
    if not isinstance(data, dict):
        raise LiquidacionesAPIError("Se esperaba el listado de facturas de XM")
    return data


def subir_facturas_xm(
    archivos: list[tuple[str, bytes, str]],
    version: str,
) -> dict[str, Any]:
    """Sube un lote de facturas en PDF. El mes y el año los extrae la IA del PDF.

    ``archivos`` son tuplas ``(nombre, contenido, content_type)``.
    """
    if not archivos:
        raise LiquidacionesAPIError("No se enviaron archivos")
    if len(archivos) > MAX_FACTURAS_POR_LOTE:
        raise LiquidacionesAPIError(
            f"La API acepta máximo {MAX_FACTURAS_POR_LOTE} facturas por lote"
        )

    # La API acepta una clave de Gemini propia para leer los PDF (§ campo
    # `api_key`, opcional). Solo se manda si está configurada en el servidor; si
    # no, esa API usa la suya. Nunca viaja desde el navegador: es un secreto.
    formulario: dict[str, str] = {"version": version}
    if settings.LIQUIDACIONES_GEMINI_API_KEY:
        formulario["api_key"] = settings.LIQUIDACIONES_GEMINI_API_KEY

    data = _request(
        "POST",
        PATH_FACTURAS_XM,
        files=[("files", (nombre, contenido, tipo)) for nombre, contenido, tipo in archivos],
        data=formulario,
    )
    if not isinstance(data, dict):
        raise LiquidacionesAPIError("La API de Liquidaciones no confirmó la subida")
    return data


# ── Datos maestros y catálogos ───────────────────────────────────────────────

# Caché de los listados pesados. `revenue_and_costs` devuelve más de 10.000
# filas sin paginar y tarda ~15 s: sin esto, cada cambio de filtro en pantalla
# vuelve a pedirlas enteras. Se invalida al escribir (repartir, subir Excel).
_CACHE_TTL_SEGUNDOS = 120
# Los costos aguantan más: traerlos todos son 22 páginas de 500 (~27 s) y solo
# cambian cuando se reparte o se sube un Excel, y las dos cosas invalidan.
_CACHE_TTL_COSTOS = 600
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def invalidar_cache() -> None:
    """Descarta lo cacheado. Se llama tras cualquier escritura que lo altere."""
    with _cache_lock:
        _cache.clear()


def _cacheado(clave: str, calcular, ttl: int = _CACHE_TTL_SEGUNDOS):
    """Devuelve el valor cacheado si sigue vigente; si no, lo recalcula."""
    ahora = time.monotonic()
    with _cache_lock:
        entrada = _cache.get(clave)
        if entrada and ahora - entrada[0] < ttl:
            return entrada[1]
    valor = calcular()
    with _cache_lock:
        _cache[clave] = (ahora, valor)
    return valor


# Paginación de los listados. Desde agosto de 2026 la API devuelve un sobre
# ``{count, next, previous, results}`` en vez de la lista pelada; sin recorrer
# ``next`` solo se verían los primeros registros. El máximo por página es 500.
_LIMITE_PAGINA = 500
# Tope de seguridad: `participants` tiene 26.000 filas y nadie quiere traerlas
# enteras por accidente. Si un listado lo supera, se corta y se avisa al log.
_MAX_PAGINAS = 60


def _listar(path: str, **filtros: Any) -> list[dict[str, Any]]:
    """GET de un listado completo, recorriendo la paginación.

    Acepta las dos formas: el sobre paginado y la lista pelada de las rutas
    viejas que todavía no lo devuelven (``/api/admin/project/``).
    """
    params = {k: v for k, v in filtros.items() if v not in (None, "")}
    params.setdefault("limit", _LIMITE_PAGINA)

    acumulado: list[dict[str, Any]] = []
    offset = 0
    for pagina in range(_MAX_PAGINAS):
        data = _request("GET", path, params={**params, "offset": offset})

        # Rutas que aún responden sin sobre: vienen completas de una.
        if isinstance(data, list):
            return data
        if not isinstance(data, dict) or "results" not in data:
            raise LiquidacionesAPIError(f"Se esperaba un listado en {path}")

        filas = data.get("results") or []
        acumulado.extend(filas)
        if not data.get("next") or not filas:
            return acumulado
        offset += len(filas)
    else:
        logger.warning(
            "%s superó %s páginas (%s filas): el listado se truncó",
            path, _MAX_PAGINAS, len(acumulado),
        )
    return acumulado


def listar_contratos() -> list[dict[str, Any]]:
    """Contratos de energía (§3.3)."""
    return _listar(PATH_CONTRATOS)


def listar_contrato_proyectos(**filtros: Any) -> list[dict[str, Any]]:
    """Vínculos contrato ↔ proyecto (§3.4)."""
    return _listar(PATH_CONTRATO_PROYECTOS, **filtros)


def listar_cantidades(**filtros: Any) -> list[dict[str, Any]]:
    """Pisos y techos de los contratos PLC (§3.5)."""
    return _listar(PATH_CANTIDADES, **filtros)


def listar_costos(**filtros: Any) -> list[dict[str, Any]]:
    """Costos e ingresos fijos por proyecto (§3.6). Cacheado: son 10.000+ filas.

    Filtros: ``project``, ``payment_type`` (el ``name`` del tipo) y ``version``.
    Traerlos todos cuesta 22 páginas de 500, de ahí la caché.
    """
    clave = "costos:" + repr(sorted((k, v) for k, v in filtros.items() if v not in (None, "")))
    return _cacheado(clave, lambda: _listar(PATH_COSTOS, **filtros), ttl=_CACHE_TTL_COSTOS)


def listar_catalogos() -> dict[str, list[dict[str, Any]]]:
    """Empresas, precios de energía y tipos de costo, para resolver los selects.

    Son datos fijos: se consultan, nunca se crean.
    """
    return _cacheado("catalogos", lambda: {
        "empresas": _listar(PATH_EMPRESAS),
        "precios_energia": _listar(PATH_PRECIOS_ENERGIA),
        "tipos_costo": _listar(PATH_TIPOS_COSTO),
    })


def crear_contrato(datos: dict[str, Any]) -> dict[str, Any]:
    """Crea un contrato de energía (§3.3)."""
    data = _request("POST", PATH_CONTRATOS, json=datos)
    if not isinstance(data, dict):
        raise LiquidacionesAPIError("La API no devolvió el contrato creado")
    return data


def vincular_contrato_proyecto(datos: dict[str, Any]) -> dict[str, Any]:
    """Vincula un contrato a un proyecto (§3.4).

    ``energy_price`` es obligatorio si la tarifa es ``ppa``, prohibido si es
    ``market`` y opcional en ``market_plus_benefits``.
    """
    data = _request("POST", PATH_CONTRATO_PROYECTOS, json=datos)
    if not isinstance(data, dict):
        raise LiquidacionesAPIError("La API no devolvió el vínculo creado")
    return data


def crear_cantidades(datos: dict[str, Any]) -> dict[str, Any]:
    """Crea un piso o un techo de un contrato PLC (§3.5): 24 valores horarios."""
    data = _request("POST", PATH_CANTIDADES, json=datos)
    if not isinstance(data, dict):
        raise LiquidacionesAPIError("La API no devolvió las cantidades creadas")
    return data


def actualizar_contrato(contrato_id: int, cambios: dict[str, Any]) -> dict[str, Any]:
    """PATCH parcial del contrato de energía: lo que no se manda no se toca."""
    data = _request("PATCH", PATH_CONTRATO.format(id=contrato_id), json=cambios)
    return data if isinstance(data, dict) else {}


def actualizar_contrato_proyecto(vinculo_id: int, cambios: dict[str, Any]) -> dict[str, Any]:
    """PATCH del vínculo contrato-proyecto (el `energy_price`, sobre todo)."""
    data = _request("PATCH", PATH_CONTRATO_PROYECTO.format(id=vinculo_id), json=cambios)
    return data if isinstance(data, dict) else {}


def actualizar_cantidades(cantidad_id: int, cambios: dict[str, Any]) -> dict[str, Any]:
    """PATCH de un piso o un techo ya creado: sus 24 horas."""
    data = _request("PATCH", PATH_CANTIDAD.format(id=cantidad_id), json=cambios)
    return data if isinstance(data, dict) else {}


# ── Ciclo mensual ────────────────────────────────────────────────────────────

def obtener_ipp(month: int, year: int) -> float:
    """IPP del mes, del DANE (§4.1). Síncrono; no se puede enviar uno propio."""
    data = _request("GET", PATH_IPP, params={"month": month, "year": year})
    if not isinstance(data, dict) or data.get("ipp") is None:
        raise LiquidacionesAPIError("La API no devolvió el IPP del período")
    return float(data["ipp"])


def _lanzar(metodo: str, path: str, **kwargs: Any) -> str:
    """Dispara una tarea asíncrona y devuelve su ``task_id``."""
    data = _request(metodo, path, **kwargs)
    if not isinstance(data, dict) or not data.get("task_id"):
        raise LiquidacionesAPIError("La API no devolvió un identificador de tarea")
    return str(data["task_id"])


def descargar_archivos_xm(month: int, year: int, version: str) -> str:
    """Descarga los ocho archivos del FTP de XM (§4.2). Requiere SIC/FRT y contratos."""
    return _lanzar("POST", PATH_FTP, json={"month": month, "year": year, "version": version})


def liquidar_contratos(month: int, year: int, version: str) -> str:
    """Liquida los contratos del período (§4.5). Va ANTES de repartir."""
    return _lanzar("POST", PATH_LIQUIDAR, json={"month": month, "year": year, "version": version})


def repartir_facturas_xm(
    month: int,
    year: int,
    total_ac_power: float,
    override: bool,
    new_version: str,
    last_version: str | None = None,
) -> str:
    """Reparte las facturas de XM entre los proyectos (§4.6).

    ``override`` debe ir en ``True`` la primera vez que se corre un período: con
    ``False`` y sin un reparto previo, la API borra los costos XM del período y
    no crea nada, sin reportar error.
    """
    cuerpo: dict[str, Any] = {
        "month": month,
        "year": year,
        "total_ac_power": total_ac_power,
        "override": override,
        "new_version": new_version,
    }
    if last_version:
        cuerpo["last_version"] = last_version
    task_id = _lanzar("POST", PATH_REPARTIR, json=cuerpo)
    # El reparto reescribe los costos del grupo xm: lo cacheado ya no sirve.
    invalidar_cache()
    return task_id


def generar_estado_resultados(month: int, year: int, version: str) -> str:
    """Genera el ``.xlsx`` del estado de resultados. Queda en Drive."""
    return _lanzar(
        "GET", PATH_ESTADO_RESULTADOS_XLSX,
        params={"month": month, "year": year, "version": version},
    )


def generar_cruce_facturas(month: int, year: int, version: str) -> str:
    """Genera el Excel que verifica que lo repartido cuadre con la factura de XM (§4.8)."""
    return _lanzar(
        "GET", PATH_CRUCE_FACTURAS,
        params={"month": month, "year": year, "version": version},
    )


def estado_resultados_json(
    month: int, year: int, version: str, project: str | None = None
) -> dict[str, Any]:
    """Estado de resultados en JSON (§4.7). Síncrono, sin sondeo.

    Ojo con ``warnings``: si un proyecto trae alguno, sus cifras están
    incompletas. Hoy casi todos traen el fallo conocido de FAZNI y cargo por
    confiabilidad, que subestima los costos e infla la utilidad.
    """
    params: dict[str, Any] = {"month": month, "year": year, "version": version}
    if project:
        params["project"] = project
    data = _request("GET", PATH_ESTADO_RESULTADOS_JSON, params=params)
    if not isinstance(data, dict):
        raise LiquidacionesAPIError("Se esperaba el estado de resultados")
    return data


def diagnosticar_proyecto(project: str, month: int, year: int, version: str) -> dict[str, Any]:
    """Responde «por qué este proyecto no sale en el estado de resultados» (§5.2)."""
    data = _request(
        "POST", PATH_DIAGNOSTICO,
        json={"project": project, "month": month, "year": year, "version": version},
    )
    if not isinstance(data, dict):
        raise LiquidacionesAPIError("Se esperaba el diagnóstico del proyecto")
    return data


def subir_excel_costos(nombre: str, contenido: bytes, tipo: str) -> dict[str, Any]:
    """Carga masiva de costos e ingresos fijos por Excel (§3.6).

    El campo del formulario se llama ``file``, en singular: un solo archivo por
    llamada. La plantilla que debería bajarse por GET de esta misma ruta hoy
    responde 400 por un fallo del lado de la API.
    """
    data = _request("POST", PATH_COSTOS_XLSX, files={"file": (nombre, contenido, tipo)})
    if not isinstance(data, dict):
        raise LiquidacionesAPIError("La API no confirmó la carga del Excel de costos")
    # La carga agrega costos: lo cacheado queda viejo.
    invalidar_cache()
    return data
