"""Tasa diaria de reporte automático (CGM) sobre las fronteras clasificadas.

Cada día: de las fronteras que el clasificador proceso, cuántas se reportaron
solas vía CGM. El total del rango es la razón de totales, no el promedio de las
tasas diarias.

**Por qué el denominador son las clasificadas.** Se probó dividir sobre las
registradas en ASIC, para que una frontera que no reporta nada penalizara en vez
de salir de los dos lados de la división. Medido contra producción el
2026-09-15, la brecha entre "debían" y "reportaron" era TODOS los días
exactamente las mismas 9 fronteras --BAYUNCA I, SAN ONOFRE, DELTA 2, NAOS 2 y 3,
con sus consumos-- que estaban en nuestra tabla y no en el catálogo de Quoia, y
que se borraron ese día. Sin ellas los dos conjuntos coinciden: ese denominador
agregaba maquinaria sin mover ningún número, y traía un desajuste propio (seis
fronteras reportaron ANTES de su `fecha_registro_asic`, lo que podía dar tasas
de más del 100%).

**Lo que sí se conserva es la alarma.** `registradas` y `sin_reportar` van en
cada día, al lado y no como divisor. Así se ve si una frontera registrada deja
de aparecer -- que es como esas 9 pasaron desapercibidas.

**Los días sin corrida quedan fuera de la tasa, pero no de la serie.** El 5 y el
6 de septiembre de 2026 no se reportó nada: fue la migración del servidor, no la
automatización (confirmado con la usuaria). Contarlos movería la métrica por dos
causas distintas sin poder saber cuál.
"""
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

    connections.close_all()
    connections.__dict__.pop("settings", None)
    connections.__init__()

    settings.MIGRATION_MODULES = {a.label: None for a in django_apps.get_app_configs()}
    setup_test_environment()
    connections["default"].creation.create_test_db(verbosity=0)
    assert connections["default"].vendor == "sqlite", "no se aisló de la base real"
    yield

    from django.test.utils import teardown_test_environment

    connections.close_all()
    teardown_test_environment()
    settings.DATABASES = originales
    connections.__dict__.pop("settings", None)
    connections.__init__()


@pytest.fixture
def base_limpia():
    from django.db import transaction

    atomica = transaction.atomic()
    atomica.__enter__()
    yield
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


from datetime import date, datetime, timezone

DIA1, DIA2, DIA3 = date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)


def _momento(d: date):
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc)


def _frontera(nombre, registrada=DIA1, borrada=None, estado="activa"):
    """`registrada` es la fecha de registro en ASIC.

    `estado="activa"` y tener código son parte del filtro -- el mismo que usa
    `orquestador._fronteras_con_reporte` para armar el reporte.
    """
    from apps.fronteras.models import Frontera

    return Frontera.objects.create(
        nombre_frontera=nombre, codigo_frontera=nombre.lower(), estado=estado,
        fecha_registro_asic=registrada,
        deleted_at=_momento(borrada) if borrada else None,
    )


def _reporte(frontera, dia, fuente="cgm"):
    """Una fila de generación: es lo que significa "el clasificador la procesó".

    `caso` es NOT NULL en las dos tablas; lo que esta métrica lee de generación
    es `medidor_usado`.
    """
    from apps.energia.models import ReporteEnergiaGeneracion

    return ReporteEnergiaGeneracion.objects.create(
        frontera_id=frontera.id, fecha=dia, medidor_usado=fuente, caso="1",
    )


def _serie(desde=DIA1, hasta=DIA3):
    from apps.energia.services.reporte.vistas import serie_automatico

    return serie_automatico(desde, hasta)


def _tasas(r):
    return [d["tasa"] for d in r["dias"]]


def _campo(r, nombre):
    return [d[nombre] for d in r["dias"]]


# ── El cálculo ──────────────────────────────────────────────────────────────


def test_la_tasa_de_un_dia_es_cgm_sobre_clasificadas(base_limpia):
    fronteras = [_frontera(f"F{i}") for i in range(4)]
    for f in fronteras:
        _reporte(f, DIA1, fuente="cgm" if f is fronteras[0] else "principal")

    assert _serie()["dias"][0]["tasa"] == 25.0


def test_el_total_es_razon_de_totales(base_limpia):
    """Cada día pesa lo que su tamaño: no es el promedio de los porcentajes."""
    fronteras = [_frontera(f"F{i}") for i in range(6)]
    # DIA1: 4 clasificadas, las 4 automáticas   -> 100%
    for f in fronteras[:4]:
        _reporte(f, DIA1, fuente="cgm")
    # DIA2: 6 clasificadas, 2 automáticas       ->  33,3%
    for i, f in enumerate(fronteras):
        _reporte(f, DIA2, fuente="cgm" if i < 2 else "principal")

    r = _serie(desde=DIA1, hasta=DIA2)

    assert r["tasa"] == 60.0, "6 automáticas de 10 clasificadas"
    promedio = round(sum(_tasas(r)) / 2, 1)
    assert promedio == 66.7, "el promedio le daría el mismo peso a los dos días"
    assert promedio != r["tasa"]


def test_una_frontera_con_generacion_y_consumo_el_mismo_dia_cuenta_una_vez(base_limpia):
    """La tasa es de FRONTERAS, no de filas: si no, una frontera con las dos
    mitades pesaría el doble y podría pasar del 100%."""
    from apps.energia.models import ReporteEnergiaConsumo

    f = _frontera("F")
    _reporte(f, DIA1, fuente="cgm")
    ReporteEnergiaConsumo.objects.create(frontera_id=f.id, fecha=DIA1, caso="CGM")

    dia1 = _serie()["dias"][0]

    assert (dia1["automaticas"], dia1["fronteras"], dia1["tasa"]) == (1, 1, 100.0)


def test_cgm_cuenta_en_mayuscula_y_en_minuscula(base_limpia):
    """Generación guarda "cgm" y consumo "CGM". Una consulta escrita a mano que
    no lo sepa cuenta de menos -- pasó el 2026-09-15 revisando esto mismo."""
    from apps.energia.models import ReporteEnergiaConsumo

    f, g = _frontera("F"), _frontera("G")
    _reporte(f, DIA1, fuente="cgm")
    ReporteEnergiaConsumo.objects.create(frontera_id=g.id, fecha=DIA1, caso="CGM")

    assert _serie()["dias"][0]["automaticas"] == 2


def test_las_otras_fuentes_no_son_automaticas(base_limpia):
    _reporte(_frontera("F"), DIA1, fuente="principal")

    assert _serie()["dias"][0]["tasa"] == 0.0


def test_el_rango_al_reves_se_rechaza():
    from api.exceptions import NoProcesable

    with pytest.raises(NoProcesable):
        _serie(desde=DIA3, hasta=DIA1)


def test_un_solo_dia_devuelve_un_solo_punto(base_limpia):
    _reporte(_frontera("F"), DIA1)

    assert len(_serie(desde=DIA1, hasta=DIA1)["dias"]) == 1


# ── Los días sin corrida ────────────────────────────────────────────────────


def _sin_corrida(r):
    return [d["fecha"] for d in r["dias"] if d["sin_corrida"]]


def test_un_dia_sin_ninguna_fila_no_entra_en_la_tasa(base_limpia):
    fronteras = [_frontera(f"F{i}") for i in range(4)]
    for dia in (DIA1, DIA2):
        for f in fronteras:
            _reporte(f, dia)
    # DIA3: el clasificador no corrió.

    r = _serie()

    assert _sin_corrida(r) == [DIA3]
    assert r["tasa"] == 100.0
    assert r["fronteras"] == 8, "el día sin corrida tampoco suma al denominador"
    assert r["dias_contados"] == 2
    assert r["dias_sin_corrida"] == 1


def test_una_corrida_a_medias_tampoco_cuenta(base_limpia):
    """El 5 de septiembre quedó UNA fila de 145. Con el denominador en las
    clasificadas eso daría `0/1` -- un cero perfecto sobre una sola frontera--
    así que se detecta por VOLUMEN: cuántas trajo el día contra la mediana."""
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    for dia in (DIA1, DIA2):
        for f in fronteras:
            _reporte(f, dia)
    _reporte(fronteras[0], DIA3, fuente="principal")

    assert _sin_corrida(_serie()) == [DIA3]


def test_una_corrida_casi_completa_si_cuenta(base_limpia):
    """Los días normales traen 136-144 filas. Faltar una frontera es un hueco de
    ESA frontera, no una corrida caída."""
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    for dia in (DIA1, DIA2):
        for f in fronteras:
            _reporte(f, dia)
    for f in fronteras[:9]:
        _reporte(f, DIA3)

    assert _sin_corrida(_serie()) == []


def test_el_dia_sin_corrida_sigue_en_la_serie(base_limpia):
    """Sale de la tasa, no de la vista: es la única forma de explicar después
    por qué ese día no hay nada."""
    for i in range(4):
        _reporte(_frontera(f"F{i}"), DIA1)

    r = _serie()

    assert len(r["dias"]) == 3
    assert r["dias"][2]["fecha"] == DIA3
    assert r["dias"][2]["registradas"] == 4, "se ve cuántas debían reportar"
    assert r["dias_sin_corrida"] == 2


def test_los_dias_malos_salen_solos(base_limpia):
    """Nadie tiene que elegir fechas a mano."""
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    for dia in (DIA1, DIA2):
        for f in fronteras:
            _reporte(f, dia, fuente="cgm")
    _reporte(fronteras[0], DIA3, fuente="principal")

    r = _serie()

    assert r["tasa"] == 100.0
    assert r["dias_sin_corrida"] == 1


def test_el_umbral_deja_margen_de_sobra():
    """Los días normales traen 136-144 filas y los rotos 1. El umbral vive en
    ese hueco, así que moverlo no cambia ningún resultado real."""
    from apps.energia.services.reporte.vistas import COBERTURA_MINIMA_DE_UNA_CORRIDA

    assert 0.01 < COBERTURA_MINIMA_DE_UNA_CORRIDA < 0.95


# ── La alarma: registradas que no aparecieron ───────────────────────────────


def test_una_frontera_registrada_que_no_reporto_se_cuenta_aparte(base_limpia):
    """Es la señal que se perdió con las 9 de BAYUNCA/NAOS/DELTA: estaban
    registradas, no reportaban, y nadie lo veía."""
    f = _frontera("Reporta")
    _frontera("Muda")
    for dia in (DIA1, DIA2, DIA3):
        _reporte(f, dia, fuente="cgm")

    r = _serie()

    assert _campo(r, "registradas") == [2, 2, 2]
    assert _campo(r, "sin_reportar") == [1, 1, 1]
    assert r["tasa"] == 100.0, "la muda NO entra en la tasa, solo en la alarma"


def test_la_brecha_es_diferencia_de_conjuntos_no_de_totales(base_limpia):
    """El 14 de septiembre había 147 registradas y 144 clasificadas, pero 6 de
    esas 144 todavía NO estaban registradas --reportaron antes de su
    `fecha_registro_asic`--, así que la resta decía 3 y faltaban 9."""
    muda = _frontera("Registrada y muda")
    temprana = _frontera("Reporta antes de registrarse", registrada=DIA3)
    _reporte(temprana, DIA1, fuente="cgm")

    dia1 = _serie()["dias"][0]

    assert dia1["registradas"] == 1, "solo la muda estaba registrada el día 1"
    assert dia1["fronteras"] == 1, "solo la temprana fue clasificada"
    assert dia1["sin_reportar"] == 1, "la muda; restar totales habría dado 0"
    assert muda.id != temprana.id


def test_una_frontera_inactiva_no_cuenta_como_faltante(base_limpia):
    """`estado` es nuestro control de apagado: una frontera que marcamos
    inactiva no reporta, así que tampoco es una ausencia."""
    _reporte(_frontera("Activa"), DIA1)
    _frontera("Apagada", estado="inactiva")

    assert _serie()["dias"][0]["sin_reportar"] == 0


def test_una_frontera_sin_codigo_no_cuenta_como_faltante(base_limpia):
    """Sin `codigo_frontera` el orquestador no la puede cruzar contra Quoia, así
    que nunca va a reportar."""
    from apps.fronteras.models import Frontera

    _reporte(_frontera("Con codigo"), DIA1)
    Frontera.objects.create(
        nombre_frontera="Sin codigo", estado="activa", fecha_registro_asic=DIA1)

    assert _serie()["dias"][0]["sin_reportar"] == 0


def test_una_frontera_sin_registro_asic_no_cuenta_como_faltante(base_limpia):
    """No se le puede exigir un reporte a algo que el mercado no conoce."""
    from apps.fronteras.models import Frontera

    _reporte(_frontera("Registrada"), DIA1)
    Frontera.objects.create(
        nombre_frontera="Sin ASIC", codigo_frontera="sinasic", estado="activa")

    assert _serie()["dias"][0]["sin_reportar"] == 0


def test_la_alta_es_el_registro_asic_y_no_la_carga_de_la_fila(base_limpia):
    """`created_at` dice cuándo alguien cargó la FILA acá. Las dos fechas no
    coinciden en NINGUNA de las 153 fronteras: casi todas se cargaron el
    2026-05-02, cuando se pobló la tabla, y BAYUNCA I está en ASIC desde 2020."""
    _frontera("Registrada despues", registrada=DIA3)

    assert _campo(_serie(), "registradas") == [0, 0, 1]


def test_una_frontera_borrada_deja_de_exigirse(base_limpia):
    _frontera("Queda")
    _frontera("Se va", borrada=DIA2)

    assert _campo(_serie(), "registradas") == [2, 1, 1]


def test_el_filtro_de_registradas_es_el_del_orquestador():
    """Si la alarma mirara un conjunto más grande que el del reporte, avisaría
    de fronteras que nunca iban a reportar."""
    import inspect

    from apps.energia.services.reporte import orquestador, vistas

    del_reporte = inspect.getsource(orquestador._fronteras_con_reporte)
    de_la_alarma = inspect.getsource(vistas._fronteras_registradas_por_dia)

    for condicion in ('estado="activa"', "codigo_frontera__isnull=False"):
        assert condicion in del_reporte
        assert condicion in de_la_alarma


# ── La tabla por frontera ───────────────────────────────────────────────────
# Reemplaza al desglose por grupo, que no decía nada: al abrir "Otra fuente"
# casi todas las filas daban 96-100%, porque esa barra es el complemento --
# cualquier frontera que no sea automática SIEMPRE tiene casi todos sus días
# ahí (reportado por la usuaria el 2026-09-15). Acá el ORDEN es la información.


def _por_frontera(r):
    return [(f["nombre_proyecto"], f["automaticos"], f["dias"], f["tasa"])
            for f in r["por_frontera"]]


def test_la_peor_va_primero(base_limpia):
    buena = _frontera("Siempre sola")
    mala = _frontera("Nunca sola")
    for dia in (DIA1, DIA2, DIA3):
        _reporte(buena, dia, fuente="cgm")
        _reporte(mala, dia, fuente="principal")

    assert _por_frontera(_serie()) == [
        ("Nunca sola", 0, 3, 0.0),
        ("Siempre sola", 3, 3, 100.0),
    ]


def test_solo_cuenta_los_dias_con_corrida(base_limpia):
    """La tabla y la tasa tienen que mirar los mismos días, o el detalle
    contradice al titular."""
    fronteras = [_frontera(n) for n in ("F", "Otra", "Tercera")]
    for dia in (DIA1, DIA2):
        for f in fronteras:
            _reporte(f, dia, fuente="cgm")
    _reporte(fronteras[0], DIA3, fuente="principal")  # día sin corrida: 1 de 3

    r = _serie()

    assert r["dias_sin_corrida"] == 1
    assert _por_frontera(r) == [
        ("F", 2, 2, 100.0), ("Otra", 2, 2, 100.0), ("Tercera", 2, 2, 100.0),
    ], "el día sin corrida no le suma un día malo a F"


def test_una_frontera_que_no_reporto_nunca_no_aparece(base_limpia):
    """No tiene filas, así que no hay días sobre los cuales sacarle un
    porcentaje. Su ausencia se cuenta en `sin_reportar`, que es la alarma."""
    f = _frontera("Reporta")
    _frontera("Muda")
    for dia in (DIA1, DIA2, DIA3):
        _reporte(f, dia)

    r = _serie()

    assert [n for n, _, _, _ in _por_frontera(r)] == ["Reporta"]
    assert _campo(r, "sin_reportar") == [1, 1, 1]
