"""Qué días quedan fuera de la tasa de automático del Resumen.

Los días en que el clasificador no hizo su trabajo no cuentan: contarlos
movería la métrica por causas que no son la automatización --una migración,
media corrida, un fallo del programa-- y no habría forma de saber cuál. Son
tres casos y cada uno es una REGLA, no una fecha escrita en el código, así que
el mismo fallo dentro de seis meses también sale solo:

  · `sin_corrida`           el día no trajo ni la mitad del volumen normal (5 y
                            6 de septiembre de 2026: la migración del servidor).
  · `corrida_parcial`       generación y consumo se separaron (4 de septiembre:
                            19 fronteras de consumo sin clasificar).
  · `clasificacion_fallida` corrió entero y no usó CGM en NINGUNA frontera (9
                            de agosto): un cero absoluto es el programa
                            fallando.

`_motivos_del_rango` es quien lo decide, y `resumen_ventana` lo aplica a todas
las fronteras por igual.
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


from datetime import date  # noqa: E402

DIA1, DIA2, DIA3 = date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)


def _frontera(nombre):
    from apps.fronteras.models import Frontera

    return Frontera.objects.create(
        nombre_frontera=nombre, codigo_frontera=nombre.lower(), estado="activa",
        fecha_registro_asic=DIA1,
    )


def _reporte(frontera, dia, fuente="cgm"):
    """Una fila de generación: es lo que significa "el clasificador la procesó".

    `caso` es NOT NULL en las dos tablas; lo que se lee de generación es
    `medidor_usado`.
    """
    from apps.energia.models import ReporteEnergiaGeneracion

    return ReporteEnergiaGeneracion.objects.create(
        frontera_id=frontera.id, fecha=dia, medidor_usado=fuente, caso="1",
    )


def _consumo(frontera, dia, caso="CGM"):
    from apps.energia.models import ReporteEnergiaConsumo

    return ReporteEnergiaConsumo.objects.create(
        frontera_id=frontera.id, fecha=dia, caso=caso)


def _dia_completo(fronteras, dia, cgm=True):
    for f in fronteras:
        _reporte(f, dia, fuente="cgm" if cgm else "principal")
        _consumo(f, dia, caso="CGM" if cgm else "medidor")


def _motivos(desde=DIA1, hasta=DIA3):
    """Solo los días excluidos, con su motivo."""
    from apps.energia.services.reporte.vistas import (
        _clasificadas_y_cgm_por_dia, _motivos_del_rango,
    )

    clasificadas, por_mitad, con_cgm = _clasificadas_y_cgm_por_dia(desde, hasta)
    motivos = _motivos_del_rango(desde, hasta, clasificadas, por_mitad, con_cgm)
    return {d: m for d, m in motivos.items() if m is not None}


# ── Los días sin corrida ────────────────────────────────────────────────────


def test_un_dia_sin_ninguna_fila_queda_fuera(base_limpia):
    fronteras = [_frontera(f"F{i}") for i in range(4)]
    for dia in (DIA1, DIA2):
        for f in fronteras:
            _reporte(f, dia)
    # DIA3: el clasificador no corrió.

    assert _motivos() == {DIA3: "sin_corrida"}


def test_una_corrida_a_medias_tampoco_cuenta(base_limpia):
    """El 5 de septiembre quedó UNA fila de 145. Contada, daría `0/1` -- un
    cero perfecto sobre una sola frontera-- así que se detecta por VOLUMEN:
    cuántas trajo el día contra la mediana."""
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    for dia in (DIA1, DIA2):
        for f in fronteras:
            _reporte(f, dia)
    _reporte(fronteras[0], DIA3, fuente="principal")

    assert _motivos() == {DIA3: "sin_corrida"}


def test_una_corrida_casi_completa_si_cuenta(base_limpia):
    """Los días normales traen 136-144 filas. Faltar una frontera es un hueco de
    ESA frontera, no una corrida caída."""
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    for dia in (DIA1, DIA2):
        for f in fronteras:
            _reporte(f, dia)
    for f in fronteras[:9]:
        _reporte(f, DIA3)

    assert _motivos() == {}


def test_el_umbral_deja_margen_de_sobra():
    """Los días normales traen 136-144 filas y los rotos 1. El umbral vive en
    ese hueco, así que moverlo no cambia ningún resultado real."""
    from apps.energia.services.reporte.vistas import COBERTURA_MINIMA_DE_UNA_CORRIDA

    assert 0.01 < COBERTURA_MINIMA_DE_UNA_CORRIDA < 0.95


# ── Las otras dos formas de que el clasificador falle ───────────────────────


def test_una_corrida_parcial_no_cuenta(base_limpia):
    """El 2026-09-04 hizo las 69 fronteras de generacion y dejo 19 de consumo
    sin clasificar. Ese dia marco 97% de CGM contra el ~35% habitual."""
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    _dia_completo(fronteras, DIA1)
    _dia_completo(fronteras, DIA2)
    # DIA3: toda la generacion, solo 3 de 10 de consumo.
    for f in fronteras:
        _reporte(f, DIA3, fuente="cgm")
    for f in fronteras[:3]:
        _consumo(f, DIA3)

    assert _motivos() == {DIA3: "corrida_parcial"}


def test_el_equilibrio_no_se_rompe_porque_entren_fronteras(base_limpia):
    """Las fronteras se van sumando --52 en agosto, 73 en septiembre-- asi que
    un umbral contra la mediana del rango marcaria los dias viejos como
    parciales. La proporcion entre mitades no se entera del crecimiento."""
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    _dia_completo(fronteras[:4], DIA1)   # dia chico, pero equilibrado
    _dia_completo(fronteras[:7], DIA2)
    _dia_completo(fronteras, DIA3)

    assert _motivos() == {}


def test_faltar_una_frontera_de_consumo_no_es_una_corrida_parcial(base_limpia):
    """Un hueco de UNA frontera es un problema de esa frontera, no del
    clasificador. Un dia normal trae 69 y 67."""
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    for dia in (DIA1, DIA2, DIA3):
        for f in fronteras:
            _reporte(f, dia, fuente="cgm")
        for f in fronteras[:9]:
            _consumo(f, dia)

    assert _motivos() == {}


def test_un_dia_con_cero_cgm_no_cuenta(base_limpia):
    """"Eso nunca pasa" (confirmado con la usuaria el 2026-09-16, sobre el 9 de
    agosto): un cero absoluto es el programa fallando, no una jornada sin
    automatizacion. Ese dia corrio ENTERO --103 filas, el volumen normal de su
    epoca-- asi que ninguna otra regla lo agarraba."""
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    _dia_completo(fronteras, DIA1)
    _dia_completo(fronteras, DIA2)
    _dia_completo(fronteras, DIA3, cgm=False)  # corrida completa, cero CGM

    assert _motivos() == {DIA3: "clasificacion_fallida"}


def test_una_sola_frontera_en_cgm_ya_no_es_un_fallo(base_limpia):
    """La regla es el CERO absoluto, no "poco". Un dia flojo es un dato."""
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    _dia_completo(fronteras, DIA1)
    _dia_completo(fronteras, DIA2)
    for f in fronteras:
        _reporte(f, DIA3, fuente="cgm" if f is fronteras[0] else "principal")
        _consumo(f, DIA3, caso="medidor")

    assert _motivos() == {}


def test_cada_dia_excluido_dice_por_que(base_limpia):
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    _dia_completo(fronteras, DIA1)
    _dia_completo(fronteras, DIA2, cgm=False)   # cero CGM
    _reporte(fronteras[0], DIA3)                # casi nada

    assert _motivos() == {DIA2: "clasificacion_fallida", DIA3: "sin_corrida"}


def test_un_dia_sin_corrida_no_se_reporta_ademas_como_cero_cgm(base_limpia):
    """Las reglas se evaluan en orden y la primera manda: un dia que no corrio
    tambien tiene cero CGM, y decir "clasificacion fallida" mandaria a buscar
    un bug donde lo que hubo fue una caida."""
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    _dia_completo(fronteras, DIA1)
    _dia_completo(fronteras, DIA2)

    assert _motivos()[DIA3] == "sin_corrida"


def test_el_equilibrio_se_calcula_igual_en_los_dos_sentidos():
    from apps.energia.services.reporte.vistas import _equilibrio

    assert _equilibrio(69, 48) == _equilibrio(48, 69)
    assert round(_equilibrio(69, 67), 2) == 0.97
    assert round(_equilibrio(69, 48), 2) == 0.70
    assert _equilibrio(0, 0) == 1.0, "sin filas no hay desequilibrio que reportar"
