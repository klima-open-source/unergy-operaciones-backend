"""Los seis endpoints de fallas del árbol Django, ejercidos de verdad.

Los 65 tests de `tests/test_fallas*.py` prueban FastAPI (`app/api/v1/fallas.py`).
Django/DRF ya lo reemplazó en producción y **ningún test importaba el árbol
nuevo**, así que estos tres 500 vivieron en producción sin que nada los viera:

  * `POST /api/v1/fallas` — `ValueError` al construir `FallaCrearSerializer`
    (`default=None` sobre `intervalos`, que es un m2m inverso). 500 en TODA
    petición, antes de validar nada.
  * `PATCH /api/v1/fallas/{id}` — los cinco `*_id` de FK caían en `ReadOnlyField`,
    así que `validated_data` salía sin ellos: 200 con el cuerpo viejo, sin cambio
    de estado, sin `fecha_resolucion` ni `sla_cumplido`, y el bloqueo de
    "pendiente de reclasificar" era código muerto en esa ruta.
  * `GET /api/v1/fallas/por-proyecto` — `falla.dias_abierta` ya no existe en el
    modelo (la lógica vive en `dominio`). Solo se veía con `items` no vacío.

Más dos derivas de forma: los `Decimal` salían como string ("2205.225") y la
paginación usaba 50/500 en vez de los 20/5000 de FastAPI, con el alias
`page_size` roto.

No hay `pytest-django`: el fixture arma una base sqlite en memoria con
`create_test_db` y `MIGRATION_MODULES` a `None` para todas las apps, o sea las
tablas salen del estado actual de los modelos. Se salta así la cadena de
migraciones de Django, que hoy no corre desde cero (`OportunidadOfertaProyecto`
tiene un pk compuesto que su 0001 no refleja) — un problema de otra app, ajeno a
esto.
"""
import json
from datetime import date

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)

    from django.conf import settings

    originales = settings.DATABASES
    settings.DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    }
    django.setup()

    from django.apps import apps as django_apps
    from django.db import connections
    from django.test.utils import setup_test_environment

    # Si otro modulo del suite ya llamo `django.setup()`, el `ConnectionHandler`
    # tiene `settings` cacheado (es un `cached_property`) y el parche de arriba
    # llega tarde: `create_test_db` se iria contra el Postgres real a crear
    # `test_operaciones`. Hay que invalidar el cache y soltar la conexion viva.
    connections.close_all()
    connections.__dict__.pop("settings", None)  # el cached_property
    connections.__init__()  # rehace `_settings` y el Local de conexiones

    settings.MIGRATION_MODULES = {a.label: None for a in django_apps.get_app_configs()}
    setup_test_environment()
    connections["default"].creation.create_test_db(verbosity=0)
    assert connections["default"].vendor == "sqlite", "no se aisló de la base real"
    yield

    # Devolver el handler a la configuración real: si no, el módulo que corra
    # después de este se encuentra la base en sqlite sin haberlo pedido.
    from django.test.utils import teardown_test_environment

    connections.close_all()
    teardown_test_environment()
    settings.DATABASES = originales
    connections.__dict__.pop("settings", None)
    connections.__init__()


@pytest.fixture
def datos():
    """Un proyecto, los catálogos mínimos y un usuario admin.

    Sin `pytest-django` no hay aislamiento automático: la transacción se abre acá
    y se revierte al terminar, para que la base en memoria vuelva vacía y los
    tests no se pisen (`usuarios.email` es único).
    """
    from django.db import transaction

    from apps.monitoreo import models as mo
    from apps.plataforma.models import Usuario
    from apps.proyectos.models import Proyecto

    atomica = transaction.atomic()
    atomica.__enter__()
    proyecto = Proyecto.objects.create(nombre_comercial="Planta Prueba")
    abierto = mo.FallaCatEstado.objects.create(
        codigo="abierta", etiqueta="Abierta", orden=1, es_estado_final=False
    )
    cerrado = mo.FallaCatEstado.objects.create(
        codigo="cerrada", etiqueta="Cerrada", orden=9, es_estado_final=True
    )
    alta = mo.FallaCatPrioridad.objects.create(codigo="alta", etiqueta="Alta", nivel=1)
    usuario = Usuario.objects.create(
        nombre="QA", email="qa@unergy.io", rol="admin", activo=True
    )
    yield {
        "proyecto": proyecto, "abierto": abierto, "cerrado": cerrado,
        "alta": alta, "usuario": usuario,
    }
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _pedir(metodo, url, datos_usuario, cuerpo=None, **kwargs):
    """Llama al `FallaViewSet` como lo haría el router, sin levantar servidor."""
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.fallas.views import FallaViewSet

    factory = APIRequestFactory()
    peticion = getattr(factory, metodo)(url, cuerpo, format="json") if cuerpo is not None \
        else getattr(factory, metodo)(url)
    # El `Usuario` crudo no sirve: `RolePermission` lee `.roles` (lista) y el
    # modelo tiene `.rol` singular -> 403.
    force_authenticate(peticion, user=UsuarioAutenticado(datos_usuario["usuario"]))
    vista = FallaViewSet.as_view(kwargs.pop("acciones"))
    return vista(peticion, **kwargs)


def _falla(datos, **extra):
    from apps.monitoreo import models as mo

    campos = {
        # Dentro de `campos` y no como kwarg fijo del `create`: `codigo_interno`
        # es UNIQUE, asi que un test que necesite DOS fallas tiene que poder
        # pasarle otro. Con el kwarg fijo, `_falla(datos, codigo_interno=...)`
        # reventaba con "got multiple values".
        "codigo_interno": "FAL-2026-00001",
        "proyecto_id": datos["proyecto"].id,
        "estado_id": datos["abierto"].id,
        "prioridad_id": datos["alta"].id,
        "descripcion": "inversor caido",
        "fecha_identificacion": date(2026, 9, 1),
        "registrado_por_id": datos["usuario"].id,
    }
    campos.update(extra)
    falla = mo.Falla.objects.create(**campos)
    return falla


# ── P0-1 · el POST respondía 500 en toda petición ─────────────────────────────

def test_post_crea_la_falla_y_no_revienta(datos):
    respuesta = _pedir(
        "post", "/api/v1/fallas",
        datos,
        {
            "proyecto_id": datos["proyecto"].id,
            "estado_id": datos["abierto"].id,
            "prioridad_id": datos["alta"].id,
            "descripcion": "inversor caido",
            "fecha_identificacion": "2026-09-01",
        },
        acciones={"post": "create"},
    )
    assert respuesta.status_code == 201, respuesta.data
    assert respuesta.data["codigo_interno"].startswith("FAL-")


# ── P0-2 · el PATCH devolvía 200 y descartaba el estado ───────────────────────

def test_patch_de_estado_final_sella_resolucion_y_sla(datos):
    falla = _falla(datos)
    respuesta = _pedir(
        "patch", f"/api/v1/fallas/{falla.id}",
        datos, {"estado_id": datos["cerrado"].id},
        acciones={"patch": "partial_update"}, pk=falla.id,
    )
    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["estado"]["codigo"] == "cerrada"
    # Las dos que `dominio.sincronizar_resolucion` escribe al cerrar. Con los
    # `*_id` en ReadOnlyField ni se llamaba: quedaban en None.
    assert respuesta.data["fecha_resolucion"] is not None
    assert respuesta.data["sla_cumplido"] is not None


def test_patch_no_cierra_una_falla_pendiente_de_reclasificar(datos):
    falla = _falla(datos, pendiente_reclasificar=True)
    respuesta = _pedir(
        "patch", f"/api/v1/fallas/{falla.id}",
        datos, {"estado_id": datos["cerrado"].id},
        acciones={"patch": "partial_update"}, pk=falla.id,
    )
    assert respuesta.status_code == 409, respuesta.data
    falla.refresh_from_db()
    assert falla.estado_id == datos["abierto"].id


# ── P0-3 · `por-proyecto` reventaba en cuanto devolvía filas ──────────────────
#
# El test de este caso se retiro junto con el endpoint (2026-09-07): no lo
# consumia ningun flujo interno ni el frontend. El arreglo que lo motivo NO se
# perdio: `dominio.dias_abierta` / `dominio.tiempo_afectacion_horas` --las dos
# propiedades que el port a Django no habia traido-- las sigue usando el
# serializer del detalle (api/v1/fallas/serializers.py:146).


# ── P1-4 y P1-5 · forma de la respuesta ───────────────────────────────────────

def test_los_decimales_salen_como_numero_no_como_string(datos):
    falla = _falla(datos, kwh_perdidos_estimado="2205.225")
    respuesta = _pedir(
        "get", f"/api/v1/fallas/{falla.id}", datos,
        acciones={"get": "retrieve"}, pk=falla.id,
    )
    assert respuesta.status_code == 200, respuesta.data
    # Sobre el JSON renderizado y no sobre `.data`, que ahi sigue siendo Decimal:
    # lo que importa es lo que sale por el cable. `COERCE_DECIMAL_TO_STRING` viene
    # en True por default y mandaba "2205.225" — cualquier aritmetica del frontend
    # sobre eso da NaN.
    cuerpo = json.loads(respuesta.render().content)
    assert isinstance(cuerpo["kwh_perdidos_estimado"], float), cuerpo["kwh_perdidos_estimado"]


@pytest.mark.parametrize(
    ("consulta", "esperado"),
    [
        ("", 20),              # el default de FastAPI, no el 50 de BasePagination
        ("?page_size=4", 4),   # el alias historico, que era un no-op
        ("?size=1000", 1000),  # antes recortaba callado a max_page_size=500
    ],
)
def test_paginacion_igual_a_fastapi(datos, consulta, esperado):
    _falla(datos)
    respuesta = _pedir(
        "get", f"/api/v1/fallas{consulta}", datos, acciones={"get": "list"},
    )
    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["size"] == esperado


# ── P1-6 · el correo al cliente salia sin quien registro la falla ─────────────
#
# `POST /fallas/{id}/notificar` pasaba `getattr(request.user, "nombre", "")`, y
# `UsuarioAutenticado` NO tiene `.nombre` -- solo `id`, `roles` y `usuario` (ver
# api/authentication.py). El `getattr` con default no fallaba: devolvia "", asi
# que el campo "registrado por" del correo que ve el cliente iba vacio, y la
# linea de log tambien. El idioma del repo es `request.user.usuario.nombre`.

def test_notificar_manda_el_nombre_de_quien_registro(datos, monkeypatch):
    from apps.monitoreo.services.fallas import notificacion

    capturado = {}

    def _falso_envio(**kwargs):
        capturado.update(kwargs)
        return {"ok": True, "enviados": ["cliente@ejemplo.com"], "errores": []}

    # Sin correos operacionales el servicio corta antes de armar el mensaje.
    monkeypatch.setattr(notificacion, "correos_de",
                        lambda *a, **k: ["cliente@ejemplo.com"])
    import app.services.email_service as email_service
    monkeypatch.setattr(email_service, "send_falla_notification_email", _falso_envio)

    falla = _falla(datos)
    respuesta = _pedir(
        "post", f"/api/v1/fallas/{falla.id}/notificar", datos, {},
        acciones={"post": "notificar"}, pk=falla.id,
    )
    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["ok"] is True
    # Lo que iba vacio.
    assert capturado["registrado_por"] == "QA", capturado.get("registrado_por")


# ── P1-7 · `GET /fallas` respondia 500 con una fecha mal formada ──────────────
#
# Los tres parametros de fecha del listado iban CRUDOS del query string al ORM.
# Django levanta ahi un `django.core.exceptions.ValidationError`, que no es de
# DRF: su `EXCEPTION_HANDLER` no lo traduce y sale un 500 -- peor que un error de
# validacion, porque parece una caida del servidor. FastAPI los declaraba
# `date | None` y devolvia 422 solo con verlos en la firma. Ahora pasan por
# `par.fecha`. Los enteros ya estaban cubiertos por `par.entero`.

@pytest.mark.parametrize("parametro", [
    "activa_en_fecha", "fecha_programada_desde", "fecha_programada_hasta",
])
@pytest.mark.parametrize("valor", ["basura", "2026-13-45", "01/09/2026"])
def test_una_fecha_mal_formada_da_422_y_no_500(datos, parametro, valor):
    _falla(datos)
    respuesta = _pedir(
        "get", f"/api/v1/fallas?{parametro}={valor}", datos, acciones={"get": "list"},
    )
    assert respuesta.status_code == 422, respuesta.data


def test_las_fechas_validas_siguen_filtrando(datos):
    from datetime import date as _date

    # Identificada el 1 de septiembre, programada para el 10.
    _falla(datos, fecha_programada=_date(2026, 9, 10))

    # Dentro del rango programado.
    r = _pedir("get", "/api/v1/fallas?fecha_programada_desde=2026-09-01"
               "&fecha_programada_hasta=2026-09-30", datos, acciones={"get": "list"})
    assert r.status_code == 200, r.data
    assert r.data["total"] == 1

    # Fuera del rango.
    r = _pedir("get", "/api/v1/fallas?fecha_programada_desde=2026-10-01",
               datos, acciones={"get": "list"})
    assert r.status_code == 200, r.data
    assert r.data["total"] == 0

    # `activa_en_fecha`: abierta desde el 1, asi que el 5 seguia activa.
    r = _pedir("get", "/api/v1/fallas?activa_en_fecha=2026-09-05",
               datos, acciones={"get": "list"})
    assert r.status_code == 200, r.data
    assert r.data["total"] == 1


# ── P1-8 · `stats/resumen` contaba las fallas borradas ────────────────────────
#
# El arreglo y sus dos tests se retiraron el 2026-09-08 junto con el endpoint:
# `stats_resumen` calculaba numeros que ninguna pantalla mostraba, y este bug
# --contaba las fallas soft-borradas-- fue justo el argumento para retirarlo. Se
# arreglo un contador que nadie miraba. Ver el commit del retiro.


# ── P0-9 · el SLA se anclaba a medianoche e ignoraba la hora ──────────────────
#
# `limite_sla` armaba el vencimiento desde `fecha_identificacion` a las 00:00 y
# NO sumaba `hora_identificacion`, que si se captura y si se guarda. Una critica
# (SLA 8 h) identificada a las 9:00 a.m. vencia a las 8:00 a.m. del MISMO dia --
# nacia vencida. De `limite_sla` salen el `sla_cumplido` que se sella al cerrar,
# el "en riesgo"/"vencido" del tablero y el badge "Cumplido/Incumplido" del
# detalle, asi que los cuatro estaban mal a la vez.
#
# La regla vive ahora en `dominio.inicio_sla`, que tambien usa el promedio de
# resolucion del tablero -- antes calculaba su propia medianoche aparte.

def test_inicio_sla_suma_la_hora_de_identificacion(datos):
    from datetime import time as _time

    from apps.monitoreo.services.fallas import dominio

    falla = _falla(datos, fecha_identificacion=date(2026, 9, 1),
                   hora_identificacion=_time(9, 30))
    inicio = dominio.inicio_sla(falla)
    assert (inicio.hour, inicio.minute) == (9, 30), inicio
    # Prioridad nivel 1 = 8 h -> vence 17:30 del mismo dia, no 08:00.
    limite = dominio.limite_sla(falla)
    assert (limite.hour, limite.minute) == (17, 30), limite


def test_sin_hora_de_identificacion_se_ancla_a_medianoche(datos):
    from apps.monitoreo.services.fallas import dominio

    # Se conserva el comportamiento anterior a proposito: caer a `created_at`
    # haria que una falla vieja cargada meses despues arrancara su SLA en la
    # fecha de carga. Hoy la unica fuente sin hora es la app movil.
    falla = _falla(datos, fecha_identificacion=date(2026, 9, 1),
                   hora_identificacion=None)
    inicio = dominio.inicio_sla(falla)
    assert (inicio.hour, inicio.minute) == (0, 0), inicio


def test_una_critica_de_la_manana_ya_no_nace_vencida(datos):
    """El efecto visible: el badge Cumplido/Incumplido del detalle."""
    from datetime import datetime as dt, time as _time, timezone as tz

    falla = _falla(datos, fecha_identificacion=date(2026, 9, 1),
                   hora_identificacion=_time(9, 0))

    # Resuelta a las 15:00 de Colombia (20:00 UTC) del mismo dia. Con SLA de 8 h
    # desde las 9:00, el limite son las 17:00 COL: cumplio. Con el ancla a
    # medianoche el limite eran las 08:00 COL y salia INCUMPLIDO.
    respuesta = _pedir(
        "patch", f"/api/v1/fallas/{falla.id}",
        datos,
        {
            "estado_id": datos["cerrado"].id,
            "fecha_resolucion": dt(2026, 9, 1, 20, 0, tzinfo=tz.utc).isoformat(),
        },
        acciones={"patch": "partial_update"}, pk=falla.id,
    )
    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["sla_cumplido"] is True, respuesta.data["sla_cumplido"]


def test_el_post_acepta_la_hora_como_la_manda_el_movil(datos):
    """Cierra el ciclo movil -> backend: `<input type="time">` manda "HH:MM"."""
    respuesta = _pedir(
        "post", "/api/v1/fallas",
        datos,
        {
            "proyecto_id": datos["proyecto"].id,
            "estado_id": datos["abierto"].id,
            "prioridad_id": datos["alta"].id,
            "descripcion": "inversor caido",
            "fecha_identificacion": "2026-09-01",
            "hora_identificacion": "09:30",
        },
        acciones={"post": "create"},
    )
    assert respuesta.status_code == 201, respuesta.data
    assert str(respuesta.data["hora_identificacion"]).startswith("09:30")

    # Y que de verdad mueva el reloj del SLA, no solo que se guarde.
    from apps.monitoreo.models import Falla
    from apps.monitoreo.services.fallas import dominio

    limite = dominio.limite_sla(Falla.objects.get(pk=respuesta.data["id"]))
    assert (limite.hour, limite.minute) == (17, 30), limite


# ── P0-10 · el reloj del SLA vivia copiado en cuatro vistas del frontend ──────
#
# `limite_sla` ya usa la hora, pero el porcentaje que se ve en las fallas
# ABIERTAS lo calculaba el navegador, y las cuatro copias anclaban a
# `fecha_identificacion + 'T00:00:00'`. Peor: contaban desde `fecha_ocurrencia`
# mientras el limite salia de la identificacion, o sea que numerador y
# denominador median desde puntos distintos. Ahora el backend expone
# `sla_horas_transcurridas` y `sla_pct`, y las vistas los LEEN.

def test_las_horas_del_sla_cuentan_desde_la_identificacion_con_hora(datos):
    from datetime import datetime as dt, time as _time, timezone as tz

    from apps.monitoreo.services.fallas import dominio

    # Identificada 9:00 COL, resuelta 15:00 COL -> 6 h, no 15 desde medianoche.
    falla = _falla(datos, fecha_identificacion=date(2026, 9, 1),
                   hora_identificacion=_time(9, 0),
                   fecha_resolucion=dt(2026, 9, 1, 20, 0, tzinfo=tz.utc))
    assert dominio.horas_transcurridas_sla(falla) == 6.0
    # Prioridad nivel 1 = 8 h -> 6/8 = 75 %.
    assert dominio.sla_pct(falla) == 75


def test_el_reloj_del_sla_ignora_fecha_ocurrencia(datos):
    """El SLA es un compromiso de ATENCION: no corre antes de identificar."""
    from datetime import datetime as dt, time as _time, timezone as tz

    from apps.monitoreo.services.fallas import dominio

    falla = _falla(datos, fecha_identificacion=date(2026, 9, 1),
                   hora_identificacion=_time(9, 0),
                   # Ocurrio tres dias antes: no debe sumar al reloj.
                   fecha_ocurrencia=dt(2026, 8, 29, 12, 0, tzinfo=tz.utc),
                   fecha_resolucion=dt(2026, 9, 1, 20, 0, tzinfo=tz.utc))
    assert dominio.horas_transcurridas_sla(falla) == 6.0


def test_el_pct_del_sla_tiene_tope_110(datos):
    from datetime import datetime as dt, timezone as tz

    from apps.monitoreo.services.fallas import dominio

    # Resuelta un mes despues con SLA de 8 h: sin tope daria miles por ciento.
    falla = _falla(datos, fecha_identificacion=date(2026, 9, 1),
                   fecha_resolucion=dt(2026, 10, 1, 12, 0, tzinfo=tz.utc))
    assert dominio.sla_pct(falla) == 110


def test_el_serializer_expone_el_reloj_del_sla(datos):
    """Lo que las cuatro vistas leen en vez de recalcular."""
    from datetime import time as _time

    falla = _falla(datos, fecha_identificacion=date(2026, 9, 1),
                   hora_identificacion=_time(9, 0))
    respuesta = _pedir(
        "get", f"/api/v1/fallas/{falla.id}", datos,
        acciones={"get": "retrieve"}, pk=falla.id,
    )
    assert respuesta.status_code == 200, respuesta.data
    assert "sla_horas_transcurridas" in respuesta.data
    assert "sla_pct" in respuesta.data
    # Abierta: el reloj corre, asi que hay numero (no None).
    assert respuesta.data["sla_horas_transcurridas"] is not None

    # Y tambien en el listado, que es de donde salen los drawers.
    lista = _pedir("get", "/api/v1/fallas", datos, acciones={"get": "list"})
    assert lista.status_code == 200, lista.data
    assert "sla_pct" in lista.data["items"][0]


# ── P1-11 · el SLA contractual del Anexo 4 se calculaba en el navegador ──────
#
# `calcSLA` vivia en `InformesMensualesPanel.vue`. Es OTRO SLA, no el operativo:
# va en DIAS y su umbral sale de la CATEGORIA de la falla, no de su prioridad.
# Se movio a `sla_contractual.py` el 2026-09-08 -- un calculo que decide lo que
# ve el cliente no debe estar en una vista, donde nadie lo cubre.
#
# Su bug era la columna "DIAS ABIERTA": contaba hasta HOY con `Date.now()`, sin
# mirar `fecha_resolucion`, asi que una falla cerrada en un dia pero identificada
# tres meses atras imprimia "90d" en el informe del cliente.

def _sla_c(datos, **extra):
    from apps.monitoreo.services.fallas import sla_contractual
    return sla_contractual.evaluar(_falla(datos, **extra))


def test_el_sla_contractual_cuenta_los_dias_que_estuvo_abierta(datos):
    from datetime import datetime as dt, timezone as tz

    # Identificada el 1 de junio, cerrada el 2: un dia, no los ~100 hasta hoy.
    r = _sla_c(datos, fecha_identificacion=date(2026, 6, 1),
               estado_id=datos["cerrado"].id,
               fecha_resolucion=dt(2026, 6, 2, 12, 0, tzinfo=tz.utc),
               clasificacion={"categoria": "red"})
    assert r["dias"] == 1, r


def test_el_sla_contractual_sin_fecha_no_inventa_dias(datos):
    """La guarda es defensiva: en la base `fecha_identificacion` es NOT NULL, asi
    que solo se puede llegar ahi con una instancia sin persistir."""
    from apps.monitoreo import models as mo
    from apps.monitoreo.services.fallas import sla_contractual

    r = sla_contractual.evaluar(mo.Falla(fecha_identificacion=None))
    assert r["dias"] is None and r["cumple"] is None, r


@pytest.mark.parametrize(("categoria", "plazo"), [
    ("red", 2),
    ("frontera", 3),
    ("inversores", 3),
    ("generando_sin_datos", 3),
    ("eventos_adversos", 4),
    ("una_categoria_nueva", 2),   # default: el plazo mas corto
])
def test_el_plazo_contractual_sale_de_la_categoria(datos, categoria, plazo):
    r = _sla_c(datos, clasificacion={"categoria": categoria})
    assert r["plazo_dias"] == plazo, r


@pytest.mark.parametrize(("codigo", "plazo"), [
    ("1.2", 3), ("4.1", 4), ("5.9", 4), ("2.7", 2),
])
def test_sin_clasificacion_cae_al_prefijo_del_tipo(datos, codigo, plazo):
    """Fallas legacy, anteriores al reporte estructurado."""
    from apps.monitoreo import models as mo

    tipo = mo.FallaCatTipo.objects.create(
        categoria=mo.FallaCatCategoria.objects.create(codigo=f"c{codigo}", etiqueta="X"),
        codigo=codigo, etiqueta="Tipo legacy",
    )
    r = _sla_c(datos, clasificacion=None, tipo_id=tipo.id)
    assert r["plazo_dias"] == plazo, r


def test_una_abierta_dentro_del_plazo_cumple(datos):
    from apps.plataforma.services.fechas import hoy_col
    from datetime import timedelta as td

    r = _sla_c(datos, fecha_identificacion=hoy_col() - td(days=2),
               clasificacion={"categoria": "red"})
    assert (r["dias"], r["cumple"]) == (2, True), r


def test_una_abierta_pasada_del_plazo_no_cumple(datos):
    from apps.plataforma.services.fechas import hoy_col
    from datetime import timedelta as td

    r = _sla_c(datos, fecha_identificacion=hoy_col() - td(days=3),
               clasificacion={"categoria": "red"})
    assert (r["dias"], r["cumple"]) == (3, False), r


def test_una_cerrada_tarde_NO_cumple(datos):
    """Decision del 2026-09-08: las cerradas tambien se evaluan.

    Antes `cumple` era True para toda falla en estado final, y el Anexo 4 quedaba
    incapaz de reportar un incumplimiento. Es al reves: en una cerrada el
    cumplimiento es un hecho establecido -- se sabe cuanto tardo.
    """
    from datetime import datetime as dt, timezone as tz

    r = _sla_c(datos, fecha_identificacion=date(2026, 1, 1),
               estado_id=datos["cerrado"].id,
               fecha_resolucion=dt(2026, 6, 1, 12, 0, tzinfo=tz.utc),
               clasificacion={"categoria": "red"})
    assert r["dias"] > 100, r
    assert r["cumple"] is False, r


def test_una_cerrada_a_tiempo_cumple(datos):
    from datetime import datetime as dt, timezone as tz

    # `red` da 2 dias de plazo; se cerro al dia siguiente.
    r = _sla_c(datos, fecha_identificacion=date(2026, 6, 1),
               estado_id=datos["cerrado"].id,
               fecha_resolucion=dt(2026, 6, 2, 12, 0, tzinfo=tz.utc),
               clasificacion={"categoria": "red"})
    assert (r["dias"], r["cumple"]) == (1, True), r


def test_una_cerrada_SIN_fecha_de_resolucion_no_se_juzga(datos):
    """Dato legacy real: `consultas.py` lo documenta en `activa_en_fecha`.

    `dias_abierta` cae a HOY cuando no hay `fecha_resolucion`, asi que juzgarla
    daria un incumplimiento enorme e inventado. No sabemos cuando se cerro: se
    dice que no se sabe. La regla anterior --cerrada = siempre cumple-- tapaba
    este caso.
    """
    r = _sla_c(datos, fecha_identificacion=date(2026, 1, 1),
               estado_id=datos["cerrado"].id, fecha_resolucion=None,
               clasificacion={"categoria": "red"})
    assert r["cumple"] is None, r
    assert r["dias"] is None, r
    # El plazo si se sabe: sale de la categoria.
    assert r["plazo_dias"] == 2, r


def test_el_serializer_expone_el_sla_contractual(datos):
    """Lo que el informe FMO lee en vez de recalcular."""
    falla = _falla(datos, clasificacion={"categoria": "frontera"})
    respuesta = _pedir(
        "get", f"/api/v1/fallas/{falla.id}", datos,
        acciones={"get": "retrieve"}, pk=falla.id,
    )
    assert respuesta.status_code == 200, respuesta.data
    contractual = respuesta.data["sla_contractual"]
    assert set(contractual) == {"dias", "plazo_dias", "etiqueta", "cumple"}, contractual
    assert contractual["plazo_dias"] == 3, contractual
    assert contractual["etiqueta"] == "Grave (66-90%)", contractual


# ── P1-12 · la paginacion recortaba callado y `updated_at` se quedaba atras ──

@pytest.mark.parametrize("consulta", [
    "?size=99999",       # sobre el tope de 5000
    "?size=0",           # bajo el minimo
    "?size=abc",         # no es entero
    "?page_size=99999",  # el alias tiene el mismo tope
    "?page=0",           # FastAPI declaraba ge=1
])
def test_una_paginacion_fuera_de_rango_da_422(datos, consulta):
    """FastAPI devolvia 422; DRF recortaba callado y el cliente no se enteraba."""
    _falla(datos)
    respuesta = _pedir(
        "get", f"/api/v1/fallas{consulta}", datos, acciones={"get": "list"},
    )
    assert respuesta.status_code == 422, respuesta.data


def test_el_tope_valido_sigue_pasando(datos):
    """El limite exacto no se rechaza: `le=5000` es inclusivo."""
    _falla(datos)
    respuesta = _pedir(
        "get", "/api/v1/fallas?size=5000", datos, acciones={"get": "list"},
    )
    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["size"] == 5000


def test_el_borrado_logico_mueve_updated_at(datos):
    """`auto_now` NO se escribe si el campo no esta en `update_fields`.

    Se atrasa `updated_at` con `queryset.update()`, que NO dispara `auto_now`, en
    vez de comparar contra el instante de creacion: en Windows el reloj puede dar
    el mismo valor para dos llamadas seguidas y la comparacion salia flaky.
    """
    from datetime import datetime as dt, timezone as tz

    from apps.monitoreo.models import Falla

    falla = _falla(datos)
    viejo = dt(2020, 1, 1, tzinfo=tz.utc)
    Falla.objects.filter(pk=falla.id).update(updated_at=viejo)
    assert Falla.objects.get(pk=falla.id).updated_at == viejo, "el atraso no quedo"

    respuesta = _pedir("delete", f"/api/v1/fallas/{falla.id}", datos,
                       acciones={"delete": "destroy"}, pk=falla.id)
    assert respuesta.status_code == 204

    despues = Falla.objects.get(pk=falla.id)
    assert despues.deleted_at is not None
    assert despues.updated_at > viejo, despues.updated_at
