"""Las rutas que se pisan entre sí resuelven a la vista correcta.

La paridad de `test_paridad_urls.py` compara CONJUNTOS de rutas: detecta una que
falta o una de más, pero no que dos se solapen. El solapamiento es el fallo
silencioso de DRF — `factura/{periodo}` con un lookup permisivo se traga
`factura/2026-06/file`, y la respuesta es un 404 o, peor, la vista equivocada
con un período llamado "file".

Acá van solo los casos AMBIGUOS: una ruta con parámetro que comparte prefijo con
otra más específica. Las rutas sin ambigüedad no necesitan estar.
"""

import os

import pytest

# (ruta, nombre de la acción que debe atenderla)
CASOS = [
    # `factura/{periodo}` contra sus cuatro sub-rutas.
    ("/api/v1/om/factura/2026-06", "factura"),
    ("/api/v1/om/factura/2026-06/file", "factura_file"),
    ("/api/v1/om/factura/2026-06/upload", "factura_upload"),
    ("/api/v1/om/factura/2026-06/enlace", "factura_enlace"),
    ("/api/v1/om/factura/2026-06/sin-match/7/asignar", "asignar_sin_match"),
    # `ipc/{año}` contra `ipc/pendiente`: solo el numérico es el año.
    ("/api/v1/om/ipc/pendiente", "ipc_pendiente"),
    ("/api/v1/om/ipc/2025", "ipc_upsert"),
    # `seleccion/{periodo}` contra la de detalle con dos segmentos más.
    ("/api/v1/om/seleccion/2026-06", "seleccion"),
    ("/api/v1/om/seleccion/2026-06/12/facturado", "facturado"),
    # `asic/{pk}` contra las acciones de lista con nombre literal.
    ("/api/v1/asic/modificacion", "modificacion"),
    ("/api/v1/asic/terminacion", "terminacion"),
    ("/api/v1/asic/gescon/diccionario", "diccionario"),
    ("/api/v1/asic/7", "partial_update"),
    # `retos/{pk}` contra `retos/metricas/{pk}`.
    ("/api/v1/retos/metricas/9", "partial_update"),
    ("/api/v1/retos/9", "partial_update"),
    # `operadores-red/{pk}` contra `operadores-red/contactos/{pk}`.
    ("/api/v1/operadores-red/contactos/3", "partial_update"),
    ("/api/v1/operadores-red/3", "partial_update"),
    # `portafolios/{pk}` contra la acción de lista `asignar`.
    ("/api/v1/portafolios/asignar", "asignar"),
    # `arriendos/documentos/{clave}` contra sus tres sub-rutas literales.
    ("/api/v1/arriendos/documentos/2026-06", "documentos"),
    ("/api/v1/arriendos/documentos/12", "documentos_eliminar"),
    ("/api/v1/arriendos/documentos/upload", "documentos_upload"),
    ("/api/v1/arriendos/documentos/upload-cuenta-cobro", "documentos_cuenta_cobro"),
    ("/api/v1/arriendos/documentos/file/12", "documentos_file"),
    ("/api/v1/arriendos/seleccion/2026-06", "seleccion"),
    ("/api/v1/arriendos/seleccion/2026-06/3/facturado", "facturado"),
    # `monitoring/{id}` contra sus sub-rutas. Las de `project/` y `fleet/` se
    # fueron con los 14 endpoints que se borraron al migrar a SolarView
    # (2026-09-03): ya no hay a quien resolverlas.
    ("/api/v1/generacion-solar/monitoring", "monitoring"),
    ("/api/v1/generacion-solar/monitoring/7", "monitoring_detalle"),
    ("/api/v1/generacion-solar/monitoring/7/inverters-power",
     "monitoring_inverters_power"),
    ("/api/v1/generacion-solar/proyecto/7/historial", "historial"),
    # `ppa/{pk}` contra sus acciones de lista con nombre literal.
    ("/api/v1/ppa/responsables", "responsables"),
    ("/api/v1/ppa/responsables/3", "responsable"),
    ("/api/v1/ppa/partes", "partes"),
    ("/api/v1/ppa/resumen-global", "resumen_global"),
    ("/api/v1/ppa/ipp/mensual", "ipp_mensual"),
    ("/api/v1/ppa/7", "partial_update"),
    ("/api/v1/ppa/7/tarifas", "tarifas"),
    # `contratos-servicio/{pk}` contra sus acciones de lista literales.
    ("/api/v1/contratos-servicio/duplicados-representacion",
     "duplicados_representacion"),
    ("/api/v1/contratos-servicio/fusionar-representacion",
     "fusionar_representacion"),
    ("/api/v1/contratos-servicio/importar-indexacion", "importar_indexacion"),
    ("/api/v1/contratos-servicio/7", "partial_update"),
    ("/api/v1/contratos-servicio/7/facturas", "facturas"),
    ("/api/v1/contratos-servicio/7/facturas/3", "factura"),
    ("/api/v1/contratos-servicio/7/pagos/3", "pago"),
    # `fronteras/{pk}` contra sus acciones de lista literales.
    ("/api/v1/fronteras/debug-quoia-border", "debug_quoia_border"),
    ("/api/v1/fronteras/quoia/pendientes", "quoia_pendientes"),
    ("/api/v1/fronteras/7", "partial_update"),
    # `liquidaciones/{pk}` contra sus acciones de lista literales.
    ("/api/v1/liquidaciones/resumen-panel", "resumen_panel"),
    ("/api/v1/liquidaciones/resumen-panel-rango", "resumen_panel_rango"),
    ("/api/v1/liquidaciones/catalogos/tipos", "catalogos"),
    ("/api/v1/liquidaciones/cargar-excel", "cargar_excel"),
    ("/api/v1/liquidaciones/7", "partial_update"),
    ("/api/v1/liquidaciones/7/informe", "informe"),
    ("/api/v1/liquidaciones/7/limpiar", "limpiar"),
    # `generacion/{pk}` contra sus acciones de lista literales.
    ("/api/v1/generacion/bulk", "bulk"),
    ("/api/v1/generacion/resumen/por-proyecto", "resumen_por_proyecto"),
    # `evo/clima/forecast` contra `evo/clima/forecast/{id}`.
    ("/api/v1/evo/clima/forecast", "clima_forecast"),
    ("/api/v1/evo/clima/forecast/12", "clima_forecast_detalle"),
    # `registros-cnd/{pk}` contra sus tres acciones de lista literales, y las
    # sub-rutas de equipos/documentos contra su propio listado.
    ("/api/v1/registros-cnd/catalogos", "catalogos"),
    ("/api/v1/registros-cnd/proyectos-disponibles", "proyectos_disponibles"),
    ("/api/v1/registros-cnd/por-proyecto/7", "por_proyecto"),
    ("/api/v1/registros-cnd/7", "retrieve"),
    ("/api/v1/registros-cnd/7/parametros-93", "parametros_93"),
    ("/api/v1/registros-cnd/7/validacion-93", "validacion_93"),
    ("/api/v1/registros-cnd/7/equipos", "equipos"),
    ("/api/v1/registros-cnd/7/equipos/3", "equipo"),
    ("/api/v1/registros-cnd/7/documentos", "documentos"),
    ("/api/v1/registros-cnd/7/documentos/3", "documento"),
    ("/api/v1/registros-cnd/7/alertas/recomputar", "recomputar_alertas"),
    ("/api/v1/registros-cnd/7/correos/CREACION_MDC_XM", "correos"),
    # `ppa/{contrato_id}` contra `ppa/resumen` y `ppa/resumen-anual`: el id se
    # acota a dígitos, así que "resumen" no puede colarse como contrato.
    ("/api/v1/cumplimiento/ppa", "ppa"),
    ("/api/v1/cumplimiento/ppa/resumen", "ppa_resumen"),
    ("/api/v1/cumplimiento/ppa/resumen-anual", "ppa_resumen_anual"),
    ("/api/v1/cumplimiento/ppa/7", "ppa_detalle"),
    ("/api/v1/cumplimiento/ppa/7/anual", "ppa_anual"),
    ("/api/v1/cumplimiento/ppa/7/plantas-inscritas-por-mes", "ppa_plantas_inscritas"),
    # `anual-matriz` contra sus dos sub-rutas.
    ("/api/v1/cumplimiento/anual-matriz", "anual_matriz"),
    ("/api/v1/cumplimiento/anual-matriz/contratos", "anual_matriz_contratos"),
    ("/api/v1/cumplimiento/anual-matriz/contrato/7", "anual_matriz_contrato"),
    # `historico/{id}` contra `historico/{id}/facturar`.
    ("/api/v1/cumplimiento/historico", "historico"),
    ("/api/v1/cumplimiento/historico/7", "historico_detalle"),
    ("/api/v1/cumplimiento/historico/7/facturar", "facturar"),
    # `clientes/{id}` contra la acción de lista `vista-comercial`, y las
    # sub-rutas del detalle contra sus propios listados.
    ("/api/v1/clientes/vista-comercial", "vista_comercial"),
    ("/api/v1/clientes/7", "retrieve"),
    ("/api/v1/clientes/7/panel", "panel"),
    ("/api/v1/clientes/7/tasas-servicio", "tasas_servicio"),
    ("/api/v1/clientes/7/tasa-servicio", "tasa_servicio"),
    ("/api/v1/clientes/7/tasa-servicio/3", "eliminar_tasa_servicio"),
    ("/api/v1/clientes/7/contactos", "contactos"),
    ("/api/v1/clientes/7/contactos/3", "contacto"),
    ("/api/v1/clientes/7/documentos", "documentos"),
    ("/api/v1/clientes/7/documentos/3", "documento"),
    ("/api/v1/clientes/7/documentos/3/archivo", "documento_archivo"),
    ("/api/v1/clientes/7/merge/9", "merge"),
    # `fallas/{id}` contra las seis acciones de lista con nombre literal, y
    # `archivos/{archivo_id}` contra su propio listado.
    ("/api/v1/fallas/catalogos", "catalogos"),
    ("/api/v1/fallas/estructura", "estructura"),
    ("/api/v1/fallas/actividad-hoy", "actividad_hoy"),
    ("/api/v1/fallas/backfill-sla", "backfill_sla"),
    ("/api/v1/fallas/7", "retrieve"),
    ("/api/v1/fallas/7/seguimientos", "seguimientos"),
    ("/api/v1/fallas/7/archivos", "archivos"),
    ("/api/v1/fallas/7/attachments", "attachments"),
    ("/api/v1/fallas/7/archivos/1a2b3c", "archivo"),
    # `comercial` no usa el detalle del router: todo cuelga de acciones de lista
    # con nombre literal, así que `ofertas/vincular-proyectos` tiene que ganarle
    # a `ofertas/{oferta_id}` (que solo acepta dígitos).
    ("/api/v1/comercial/config", "config"),
    ("/api/v1/comercial/oportunidades", "oportunidades"),
    ("/api/v1/comercial/oportunidades/7", "oportunidad"),
    ("/api/v1/comercial/oportunidades/7/estado", "oportunidad_estado"),
    ("/api/v1/comercial/oportunidades/7/gestiones", "gestiones"),
    ("/api/v1/comercial/oportunidades/7/proyectos", "oportunidad_proyectos"),
    ("/api/v1/comercial/oportunidades/7/ofertas", "oportunidad_ofertas"),
    ("/api/v1/comercial/ofertas", "ofertas"),
    ("/api/v1/comercial/ofertas/vincular-proyectos", "vincular_proyectos"),
    ("/api/v1/comercial/ofertas/7", "oferta"),
    ("/api/v1/comercial/ofertas/7/estado", "oferta_estado"),
    ("/api/v1/comercial/ofertas/7/firmar", "oferta_firmar"),
    ("/api/v1/comercial/ofertas/7/seguimiento", "oferta_seguimiento"),
    ("/api/v1/comercial/proyectos-operando", "proyectos_operando"),
    ("/api/v1/comercial/registrar", "registrar"),
    # `proyectos/{id}` contra las cinco acciones de lista con nombre literal.
    # `gen-promedio` y `gen-promedio/recalcular` conviven porque las dos están
    # ancladas: la primera no se traga a la segunda.
    ("/api/v1/proyectos/pendientes", "pendientes"),
    ("/api/v1/proyectos/pendientes/core:abc/confirmar", "confirmar_pendiente"),
    ("/api/v1/proyectos/pendientes/core:abc/ignorar", "ignorar_pendiente"),
    ("/api/v1/proyectos/gen-promedio", "listar_gen_promedio"),
    ("/api/v1/proyectos/gen-promedio/recalcular", "recalcular_gen_promedio"),
    ("/api/v1/proyectos/lista", "lista"),
    ("/api/v1/proyectos/buscar", "buscar"),
    ("/api/v1/proyectos/7", "retrieve"),
    ("/api/v1/proyectos/7/debug-generacion", "debug_generacion"),
    ("/api/v1/proyectos/7/vincular-sunfactory/99", "vincular_sunfactory"),
    ("/api/v1/proyectos/7/merge/9", "merge"),
    ("/api/v1/proyectos/7/servicios", "servicios"),
    ("/api/v1/proyectos/7/info-tecnica", "info_tecnica"),
    ("/api/v1/proyectos/7/inversores", "inversores"),
    ("/api/v1/proyectos/7/inversores/3", "inversor"),
    ("/api/v1/proyectos/7/area-contactos", "area_contactos"),
    ("/api/v1/proyectos/7/area-contactos/operacional", "area_contacto"),
    ("/api/v1/proyectos/7/inversionistas", "inversionistas"),
    ("/api/v1/proyectos/7/inversionistas/3", "inversionista"),
    # `reporte-energia` cuelga TODO de acciones de lista: `fronteras/{id}` es un
    # regex propio, no el detalle del router. Las sub-rutas literales de una
    # frontera tienen que ganarle a `fronteras/{id}`, que solo acepta dígitos.
    ("/api/v1/reporte-energia/resumen", "resumen"),
    ("/api/v1/reporte-energia/resumen-historico", "resumen_historico"),
    ("/api/v1/reporte-energia/fronteras", "fronteras"),
    ("/api/v1/reporte-energia/fronteras/7", "frontera"),
    ("/api/v1/reporte-energia/fronteras/7/rellenar-horario", "rellenar_horario"),
    ("/api/v1/reporte-energia/fronteras/7/deshacer-relleno", "deshacer_relleno"),
    ("/api/v1/reporte-energia/fronteras/7/recuperar-medidor", "recuperar_medidor"),
    ("/api/v1/reporte-energia/fronteras/7/revisar-respaldo", "revisar_respaldo"),
    ("/api/v1/reporte-energia/fronteras/7/cargar-excel-terceros", "excel_terceros"),
    ("/api/v1/reporte-energia/fronteras/7/curva-tipica", "curva_tipica"),
    ("/api/v1/reporte-energia/fronteras/7/validar", "validar"),
    ("/api/v1/reporte-energia/fronteras/7/exclusiones", "exclusiones"),
    ("/api/v1/reporte-energia/exclusiones/3", "exclusion"),
    ("/api/v1/reporte-energia/exclusiones/3/resolver", "resolver_exclusion"),
    ("/api/v1/reporte-energia/excel", "excel"),
    ("/api/v1/reporte-energia/ejecutar", "ejecutar"),
    ("/api/v1/reporte-energia/ejecutar/estado", "ejecutar_estado"),
    ("/api/v1/reporte-energia/ejecutar/cancelar", "ejecutar_cancelar"),
    ("/api/v1/reporte-energia/enviar", "enviar"),
    ("/api/v1/reporte-energia/estado-quoia", "estado_quoia"),
    # `panel-contable/{pk}` (PATCH) contra las acciones de lista con nombre
    # literal, y las de detalle con un segmento más.
    ("/api/v1/panel-contable/clasificacion", "clasificacion"),
    ("/api/v1/panel-contable/contraste", "contraste"),
    ("/api/v1/panel-contable/diferencia", "diferencia"),
    ("/api/v1/panel-contable/redividir", "redividir"),
    ("/api/v1/panel-contable/mapeo-celda", "mapeo_celda"),
    ("/api/v1/panel-contable/alias-fuente", "alias_fuente"),
    ("/api/v1/panel-contable/fuente-ingreso", "fuente_ingreso"),
    ("/api/v1/panel-contable/cargar-er", "cargar_er"),
    ("/api/v1/panel-contable/cargar-periodo", "cargar_periodo"),
    ("/api/v1/panel-contable/consecutivos-usados", "consecutivos_usados"),
    ("/api/v1/panel-contable/reasignar-consecutivos", "reasignar_consecutivos"),
    ("/api/v1/panel-contable/12", "partial_update"),
    ("/api/v1/panel-contable/12/soporte", "soporte"),
    ("/api/v1/panel-contable/12/estado-resultados", "estado_resultados"),
]


@pytest.fixture(scope="module", autouse=True)
def django_listo():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    import django

    django.setup()


@pytest.mark.parametrize("ruta,accion", CASOS)
def test_la_ruta_resuelve_a_su_accion(ruta, accion):
    from django.urls import Resolver404, resolve

    try:
        coincidencia = resolve(ruta)
    except Resolver404:
        pytest.fail(f"{ruta} no resuelve a ninguna vista")

    acciones = set((getattr(coincidencia.func, "actions", None) or {}).values())
    assert accion in acciones, (
        f"{ruta} resolvió a {sorted(acciones)} y se esperaba '{accion}'. "
        "Suele ser una ruta con parámetro registrada antes que otra más "
        "específica: revisa el orden de los @action o del router.register."
    )


# ---------------------------------------------------------------------------
# El mismo fallo, buscado a lo ancho de TODA la API
# ---------------------------------------------------------------------------

def _rutas_del_resolver():
    """`[(patrón, url_name)]` de todas las rutas de la API, sin las de formato."""
    from django.urls import get_resolver

    rutas = []

    def recorrer(resolver, prefijo=""):
        for patron in resolver.url_patterns:
            # El `^`/`$` se quita POR SEGMENTO: al concatenar quedarían dentro
            # de la ruta ("api/v1/^solar/…") y nada resolvería.
            texto = prefijo + str(patron.pattern).strip("^$")
            if hasattr(patron, "url_patterns"):
                recorrer(patron, texto)
            elif "format" not in texto:
                rutas.append((texto, patron.name))

    recorrer(get_resolver())
    return rutas


def _es_literal(patron: str) -> bool:
    """Sin grupos ni metacaracteres: la ruta es una cadena fija."""
    import re as _re

    return not _re.search(r"\(\?P<|\[|\(|\+|\*|\?|\\d|\\w", patron.strip("^$"))


def test_ninguna_ruta_con_comodin_tapa_una_ruta_literal():
    """DRF ordena las `@action` ALFABÉTICAMENTE, no por orden de declaración.

    `get_extra_actions()` hace `sorted(...)`, así que una acción llamada
    `documentos` con un `url_path` comodín se registra ANTES que
    `documentos_upload` y se traga `/documentos/upload`. El fallo es silencioso:
    la ruta existe, resuelve, y atiende la vista equivocada.

    Esta prueba lo busca sola: toma cada ruta LITERAL de la API y comprueba que
    resuelva a sí misma. No hay que acordarse de añadir casos.
    """
    from django.urls import Resolver404, resolve

    # `api-root` es la vista raíz que `DefaultRouter` añade sola: hay una por
    # cada router y todas cuelgan de `/api/v1`, así que se tapan entre sí. Es
    # una comodidad de la API navegable, no un endpoint del contrato.
    tapadas = []
    for patron, nombre in _rutas_del_resolver():
        if nombre == "api-root" or nombre is None or not _es_literal(patron):
            continue
        ruta = "/" + patron.strip("/")
        try:
            if resolve(ruta).url_name != nombre:
                tapadas.append(
                    f"{ruta} debería ser '{nombre}' y resuelve a "
                    f"'{resolve(ruta).url_name}'"
                )
        except Resolver404:
            tapadas.append(f"{ruta} ({nombre}) no resuelve")

    assert not tapadas, (
        "Rutas literales tapadas por otra con comodín registrada antes:\n  "
        + "\n  ".join(sorted(tapadas))
    )
