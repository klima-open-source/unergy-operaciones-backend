"""Las rutas portadas a Django tienen que responder EXACTAMENTE donde FastAPI.

Este es el contrato de toda la migracion: el frontend en produccion llama
`/api/v1/<recurso>` y no se entera de cual de los dos backends le contesta. Una
barra final de mas, un verbo que se perdio o una accion que quedo con otro
`url_path` son fallos invisibles hasta que alguien abre la pantalla.

La prueba compara las dos tablas de rutas para los prefijos ya portados. Al
portar un modulo se agrega su prefijo a `PREFIJOS_PORTADOS` y esta prueba pasa a
vigilarlo; mientras no este en la lista, el prefijo lo sigue sirviendo FastAPI y
no hay nada que comparar.

Un recurso **retirado** es el tercer caso, y no se expresa quitandolo de la lista
sin mas: eso lo dejaria pareciendo "todavia en FastAPI". Va en
`PREFIJOS_RETIRADOS` --si se fue el recurso completo-- o en `RUTAS_RETIRADAS`
--si el recurso sigue vivo y solo se quitaron algunos endpoints--, con su fecha y
su razon.
"""

import os
import re

import pytest

# Prefijos ya portados a Django. Una linea por modulo migrado.
PREFIJOS_PORTADOS = [
    "/api/v1/alertas",
    "/api/v1/api-keys",
    "/api/v1/arriendos",
    "/api/v1/asic",
    "/api/v1/auth",
    "/api/v1/usuarios",
    "/api/v1/clasificacion-energia",
    "/api/v1/clientes",
    "/api/v1/comercial",
    "/api/v1/contratos-servicio",
    "/api/v1/cumplimiento",
    "/api/v1/dashboard",
    "/api/v1/estados-resultados",
    "/api/v1/evo",
    "/api/v1/facturacion",
    "/api/v1/fallas",
    "/api/v1/finanzas/mandatos",
    "/api/v1/fronteras",
    "/api/v1/garantias-ajustes",
    "/api/v1/garantias/modelo",
    "/api/v1/garantias/proyecciones",
    "/api/v1/generacion",
    "/api/v1/generacion-solar",
    "/api/v1/informe-om",
    "/api/v1/informes",
    "/api/v1/liquidaciones",
    "/api/v1/liquidaciones-api",
    "/api/v1/mandato-inversionistas",
    "/api/v1/mandatos",
    "/api/v1/mapa",
    "/api/v1/monitoreo",
    "/api/v1/notificaciones",
    "/api/v1/operadores-red",
    "/api/v1/om",
    "/api/v1/panel-contable",
    "/api/v1/polizas",
    "/api/v1/ppa",
    "/api/v1/portafolios",
    "/api/v1/proximos-energizar",
    "/api/v1/proyectos",
    "/api/v1/reconectadores",
    "/api/v1/registros-cnd",
    "/api/v1/reporte-cgm",
    "/health",
    "/api/v1/reporte-energia",
    "/api/v1/retos",
    "/api/v1/solar",
    "/api/v1/starlink",
    "/api/v1/verificacion-costos",
]

# Recursos que ya NO existen en ninguno de los dos arboles. No van en
# `PREFIJOS_PORTADOS` porque no hay Django que comparar, pero tampoco es que
# "los siga sirviendo FastAPI": se eliminaron. El modulo de `app/` sigue en la
# imagen (ese arbol esta apagado, ver CLAUDE.md) y por eso el lado FastAPI de
# esta prueba todavia los enumera; sin esta lista, el test los reportaria como
# rutas perdidas.
#
#   /api/v1/mantenimiento-impacto  2026-09-07  tabla en 0 filas y ningun
#       consumidor: ni la bandera `generar_impacto`, ni los 5 endpoints, ni los
#       4 campos que derivaba `cumplimiento/resumen.py` aparecian en el
#       frontend. Ver apps/monitoreo/migrations/0005_eliminar_mantenimiento_impacto.py
PREFIJOS_RETIRADOS = [
    "/api/v1/mantenimiento-impacto",
]

# Rutas SUELTAS retiradas. No van arriba porque su recurso sigue vivo: quitar
# `/api/v1/fallas` de la vigilancia por tres endpoints dejaria los otros quince
# sin comparar. Llevan el verbo porque un mismo path puede conservar unos metodos
# y perder otros.
#
#   GET /fallas/sla-dashboard, GET /fallas/stats/resumen, GET /fallas/{}/impacto
#       2026-09-08  Ninguna pantalla los consumia --no estan en el `RUTAS` de
#       `features/fallas/services/fallas.ts`-- y el unico integrador externo vivo
#       (la API Key "Api Fallas", usada el 2026-08-20) solo crea y consulta
#       fallas. El de impacto ademas ESCRIBIA en un GET, congelando un numero
#       provisional calculado con `now()` (§5.9 de ARQUITECTURA_MONITOREO.md).
RUTAS_RETIRADAS = {
    ("/api/v1/fallas/sla-dashboard", "GET"),
    ("/api/v1/fallas/stats/resumen", "GET"),
    ("/api/v1/fallas/{}/impacto", "GET"),
}


# Funcionalidad NUEVA, que nunca existio en FastAPI. Es el cuarto caso: no es una
# ruta portada, ni retirada, ni un `url_path` mal escrito. Se declara aqui a
# proposito --con fecha y razon-- para que agregar una siga siendo una decision
# consciente y no algo que se cuela sin que nadie lo note.
#
#   2026-09-09  PATCH del contrato de energia. La plataforma solo sabia crear
#               contratos; corregir uno obligaba a entrar al Django de
#               Liquidaciones. La API externa si lo permite (`OPTIONS` sobre el
#               detalle responde GET, PUT, PATCH), solo faltaba exponerlo.
#   2026-09-10  POST /ipp/sincronizar. El IPP del DANE se consultaba en dos
#               sitios que no se hablaban: la API de Liquidaciones y nuestro
#               `ipp_mensual`, que se llenaba a mano y estaba 15 meses corto.
#   2026-09-11  GET /facturacion/vs-despachos. Cruza lo que DEBE entrar por
#               proyecto (lo calcula Facturacion) contra lo ya liquidado en
#               `market_settlements`. Nadie comparaba las dos cifras.
RUTAS_NUEVAS = {
    ("/api/v1/liquidaciones-api/contratos-energia/{}", "PATCH"),
    ("/api/v1/liquidaciones-api/ipp/sincronizar", "POST"),
    ("/api/v1/facturacion/vs-despachos", "GET"),
}


# Verbos que DRF agrega por su cuenta y FastAPI nunca declara.
VERBOS_IGNORADOS = {"HEAD", "OPTIONS", "TRACE"}


def _canonica(ruta: str) -> str:
    """Normaliza una ruta para poder comparar las dos sintaxis.

    `/retos/{id}` (FastAPI) y `/retos/(?P<pk>[^/.]+)` (Django) describen la misma
    ruta: lo que importa es la POSICION de los parametros, no su nombre.
    """
    ruta = re.sub(r"\(\?P<[^>]+>[^)]*\)", "{}", ruta)
    ruta = re.sub(r"\{[^}]*\}", "{}", ruta)
    return "/" + ruta.strip("^$").strip("/")


def _rutas_fastapi() -> set[tuple[str, str]]:
    """Las rutas EFECTIVAS de FastAPI, leídas del esquema OpenAPI.

    NO se recorre `app.routes`: esta versión de FastAPI no aplana los routers
    incluidos, los envuelve en un `_IncludedRouter`, así que ahí solo aparecen
    las cinco rutas de nivel raíz (`/docs`, `/health`…). Recorrerlo dejaba este
    test comparando contra una tabla casi vacía y pasando en vacío.

    `app.openapi()` es la vista pública y estable de las rutas: no depende de
    cómo la versión de turno guarde los routers por dentro.
    """
    from app.main import app

    rutas = {
        (_canonica(ruta), metodo.upper())
        for ruta, operaciones in app.openapi()["paths"].items()
        for metodo in operaciones
        if metodo.upper() not in VERBOS_IGNORADOS
    }
    return rutas | _rutas_de_modulos_stubeados()


# `tests/conftest.py` reemplaza `app.api.v1.auth` por un router VACÍO —lo
# necesitan tres pruebas que llaman endpoints sin autenticar—, así que sus ocho
# rutas no aparecen en el esquema de la app. Se leen del archivo real, cargado
# aparte y bajo otro nombre de módulo: así el stub sigue en pie para todo lo
# demás y este test ve la tabla completa.
_MODULOS_STUBEADOS = (
    ("app/api/v1/auth.py", ("router", "usuarios_router")),
)


def _rutas_de_modulos_stubeados() -> set[tuple[str, str]]:
    import importlib.util
    import os

    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rutas = set()
    for relativo, routers in _MODULOS_STUBEADOS:
        spec = importlib.util.spec_from_file_location(
            f"_paridad_{os.path.basename(relativo)[:-3]}",
            os.path.join(raiz, relativo),
        )
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        for nombre in routers:
            router = getattr(modulo, nombre)
            for ruta in router.routes:
                for metodo in getattr(ruta, "methods", None) or ():
                    if metodo not in VERBOS_IGNORADOS:
                        rutas.add((
                            _canonica(f"/api/v1{ruta.path}"),
                            metodo,
                        ))
    return rutas


def _rutas_django() -> set[tuple[str, str]]:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    import django

    django.setup()
    from django.urls import get_resolver

    rutas = set()

    def recorrer(resolver, prefijo=""):
        for patron in resolver.url_patterns:
            # El `^`/`$` se quita POR SEGMENTO. Concatenando sin quitarlo queda
            # "api/v1/^retos$", que no empieza por "/api/v1/retos" y hacía que
            # el filtro de prefijos descartara TODAS las rutas de Django.
            texto = prefijo + str(patron.pattern).strip("^$")
            if hasattr(patron, "url_patterns"):
                recorrer(patron, texto)
                continue
            # Las variantes `.json`/`.api` que agrega DefaultRouter no son rutas
            # del contrato: el frontend nunca las llama.
            if "format" in texto:
                continue
            vista = patron.callback
            acciones = getattr(vista, "actions", None) or {}
            if not acciones:
                # Vista de función (no ViewSet): no hay mapa de router del que
                # leer los verbos, así que la vista los declara ella misma en
                # `metodos_http`. Sin esto la ruta no se ve y la paridad la
                # reporta como faltante.
                for metodo in getattr(vista, "metodos_http", ()):
                    rutas.add((_canonica("/" + texto), metodo.upper()))
                continue
            # `http_method_names` de la vista MANDA sobre el mapa del router.
            # `UpdateModelMixin` registra PUT y PATCH juntos; una vista que solo
            # declara PATCH responde 405 al PUT, igual que FastAPI, así que ese
            # PUT no es una ruta de más: no existe para el cliente.
            permitidos = {
                m.lower()
                for m in getattr(getattr(vista, "cls", None),
                                 "http_method_names", None) or acciones
            }
            for metodo in acciones:
                if (
                    metodo.lower() in permitidos
                    and metodo.upper() not in VERBOS_IGNORADOS
                ):
                    rutas.add((_canonica("/" + texto), metodo.upper()))

    recorrer(get_resolver())
    return rutas


def _de_los_portados(rutas):
    def cubre(ruta, prefijos):
        return any(ruta == p or ruta.startswith(p + "/") for p in prefijos)

    return {
        (ruta, metodo) for ruta, metodo in rutas
        if cubre(ruta, PREFIJOS_PORTADOS)
        and not cubre(ruta, PREFIJOS_RETIRADOS)
        and (ruta, metodo) not in RUTAS_RETIRADAS
    }


@pytest.mark.skipif(
    not PREFIJOS_PORTADOS, reason="todavia no hay modulos portados a Django"
)
def test_las_rutas_portadas_coinciden_con_las_de_fastapi():
    fastapi = _de_los_portados(_rutas_fastapi())
    django_ = _de_los_portados(_rutas_django())

    # Un conjunto vacío haría pasar las dos comparaciones sin comparar nada.
    # Pasó de verdad: durante 26 recursos este test estuvo en verde midiendo
    # cero rutas, porque recorría `app.routes` (que en esta versión de FastAPI
    # no aplana los routers incluidos) y porque no quitaba el `^`/`$` de cada
    # segmento de Django. Un test que filtra a vacío no falla nunca.
    assert fastapi, (
        "No se leyó ninguna ruta de FastAPI para los prefijos portados. El "
        "test estaría pasando en vacío: revisa `_rutas_fastapi`."
    )
    assert django_, (
        "No se leyó ninguna ruta de Django para los prefijos portados. El test "
        "estaría pasando en vacío: revisa `_rutas_django`."
    )

    # Guardián POR PREFIJO, no solo global: si un recurso portado no aporta
    # ninguna ruta de FastAPI, su comparación es vacía aunque el total no lo
    # sea. Pasó con `auth`, que el conftest stubea.
    sin_rutas = [
        p for p in PREFIJOS_PORTADOS
        if not any(r == p or r.startswith(p + "/") for r, _ in fastapi)
    ]
    assert not sin_rutas, (
        "Estos prefijos están en PREFIJOS_PORTADOS pero no aportan ninguna "
        "ruta de FastAPI, así que no se están comparando:\n  "
        + "\n  ".join(sin_rutas)
    )

    faltan = fastapi - django_
    sobran = django_ - fastapi - RUTAS_NUEVAS

    assert not faltan, (
        "Django no expone rutas que FastAPI sí sirve — el frontend recibiría 404:\n  "
        + "\n  ".join(f"{m} {r}" for r, m in sorted(faltan))
    )
    assert not sobran, (
        "Django expone rutas que FastAPI no tiene — revisa el url_path de la acción:\n  "
        + "\n  ".join(f"{m} {r}" for r, m in sorted(sobran))
    )
