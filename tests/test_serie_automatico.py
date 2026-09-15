"""Tasa diaria de reporte automático (CGM), sobre las fronteras REPORTABLES.

La métrica de automatización contaba días-frontera sobre **las que reportaron**.
Eso dejaba dos agujeros, los dos medidos contra producción el 2026-09-15:

**1. Una frontera que no reportó nada no penalizaba.** Registrada en ASIC, una
frontera tiene que reportar todos los días aunque sea una matriz de ceros
(confirmado por la usuaria el 2026-09-15). No reportar es entonces el PEOR caso
--peor que un reporte manual-- y contando solo las que reportaron salía de los
dos lados de la división, así que era invisible. El 16 de agosto reportaron 106
de las 139 registradas.

**2. El total del rango dependía de cómo se resumiera.** Con el denominador
moviéndose día a día, el promedio de las tasas diarias y la razón de totales
daban 34,7% y 36,5%: 2 puntos de diferencia sin que ninguna fuera "la buena".
Peor con un día roto -- el 5 de septiembre el clasificador no corrió y quedó UNA
fila: `0/1` es un cero perfecto que en un promedio de días pesaba como un mes.

Con el denominador en las fronteras reportables las dos cuentas dan lo mismo, y
el día roto entra como `0/145`: se diluye solo y queda contado como lo que fue,
un día sin reportar. Por eso esta métrica no necesita que nadie saque los días
malos a mano.

Se usa la razón de totales igual, porque el denominador SÍ se mueve --entran
fronteras nuevas al ASIC-- y ahí el promedio le daría el mismo peso a un día con
147 fronteras que a uno con 139.

**La fecha que define el denominador es `fecha_registro_asic`, no `created_at`.**
El primer intento usó `created_at` y estaba mal: dice cuándo se cargó la FILA en
nuestra tabla, no cuándo la frontera empezó a deber un reporte. El 14 de
septiembre se cargaron 6 de una y el denominador saltaba de 147 a 153 sin que
cambiara nada en la realidad -- ruido nuestro disfrazado de caída de la métrica.
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
    """`registrada` es la fecha de registro en ASIC: desde ahí debe reportar.

    `estado="activa"` y tener código son parte del filtro -- el mismo que usa
    `orquestador._fronteras_con_reporte` para armar el reporte."""
    from apps.fronteras.models import Frontera

    return Frontera.objects.create(
        nombre_frontera=nombre, codigo_frontera=nombre.lower(), estado=estado,
        fecha_registro_asic=registrada,
        deleted_at=_momento(borrada) if borrada else None,
    )


def _reporte(frontera, dia, fuente="cgm"):
    from apps.energia.models import ReporteEnergiaGeneracion

    # `caso` es NOT NULL en las dos tablas; lo que esta metrica lee de
    # generacion es `medidor_usado`.
    return ReporteEnergiaGeneracion.objects.create(
        frontera_id=frontera.id, fecha=dia, medidor_usado=fuente, caso="1",
    )


def _serie(desde=DIA1, hasta=DIA3):
    from apps.energia.services.reporte.vistas import serie_automatico

    return serie_automatico(desde, hasta)


def _tasas(r):
    return [d["tasa"] for d in r["dias"]]


# ── El ejemplo que explica la métrica ───────────────────────────────────────


def test_el_ejemplo_de_diez_fronteras(base_limpia):
    """10 fronteras, 3 días. El día 3 el clasificador no corrió y quedó 1 fila.

        día 1   5 con CGM de 10 reportables   50%   cuenta
        día 2   6 con CGM de 10 reportables   60%   cuenta
        día 3   0 con CGM de 10 reportables    0%   SIN CORRIDA, no cuenta

    El día 3 se sigue viendo con su 0%, pero no entra en el total: no reportar
    por una caída del proceso no es un fallo de la automatización.
    """
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    for f in fronteras[:5]:
        _reporte(f, DIA1)
    for f in fronteras[:6]:
        _reporte(f, DIA2)
    _reporte(fronteras[0], DIA3, fuente="principal")  # la única fila del día 3

    r = _serie()

    assert _tasas(r) == [50.0, 60.0, 0.0], "los tres días se ven"
    assert [d["sin_corrida"] for d in r["dias"]] == [False, False, True]
    assert r["automaticas"] == 11
    assert r["fronteras"] == 20, "el día sin corrida no suma al denominador"
    assert r["tasa"] == 55.0


def test_con_denominador_fijo_las_dos_formas_coinciden(base_limpia):
    """Es el argumento entero del diseño: deja de haber una decisión que tomar."""
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    for f in fronteras[:5]:
        _reporte(f, DIA1)
    for f in fronteras[:6]:
        _reporte(f, DIA2)

    r = _serie()
    contados = [d["tasa"] for d in r["dias"] if not d["sin_corrida"]]
    promedio = round(sum(contados) / len(contados), 1)

    assert r["tasa"] == promedio


# ── El día que el clasificador no corrió ────────────────────────────────────


def test_un_dia_sin_ninguna_fila_se_ve_entero(base_limpia):
    """No `0/0` ni desaparecido: cero sobre las fronteras que debían reportar,
    y marcado como lo que fue."""
    for i in range(4):
        _frontera(f"F{i}")

    dia3 = _serie()["dias"][2]

    assert dia3 == {
        "fecha": DIA3, "automaticas": 0, "fronteras": 4,
        "reportaron": 0, "sin_corrida": True, "tasa": 0.0,
    }


def test_los_dias_malos_salen_solos(base_limpia):
    """Nadie tiene que elegir fechas a mano: el día sin corrida se detecta por
    su cobertura y queda fuera del total."""
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    for dia in (DIA1, DIA2):
        for f in fronteras:
            _reporte(f, dia)
    _reporte(fronteras[0], DIA3, fuente="principal")

    r = _serie()

    assert r["tasa"] == 100.0
    assert r["dias_sin_corrida"] == 1


# ── El denominador se mueve con el registro en ASIC ─────────────────────────


def test_una_frontera_registrada_a_mitad_no_cuenta_antes(base_limpia):
    """Antes del registro en ASIC no le debe un reporte a nadie."""
    _frontera("Vieja")
    _frontera("Nueva", registrada=DIA3)

    assert [d["fronteras"] for d in _serie()["dias"]] == [1, 1, 2]


def test_una_frontera_borrada_sale_del_denominador(base_limpia):
    _frontera("Queda")
    _frontera("Se va", borrada=DIA2)

    assert [d["fronteras"] for d in _serie()["dias"]] == [2, 1, 1]


def test_el_total_es_razon_de_totales_no_promedio(base_limpia):
    """Se separan cuando el denominador cambia: un día con 2 fronteras no puede
    pesar igual que uno con 1."""
    vieja = _frontera("Vieja")
    _frontera("Nueva", registrada=DIA2)
    _reporte(vieja, DIA1)
    for dia in (DIA2, DIA3):
        _reporte(vieja, dia)
        _reporte(Frontera_de("Nueva"), dia, fuente="principal")

    r = _serie()
    contados = [d["tasa"] for d in r["dias"] if not d["sin_corrida"]]
    promedio = round(sum(contados) / len(contados), 1)

    assert r["tasa"] == 60.0, "3 con CGM de 5 fronteras-día"
    assert promedio != r["tasa"], "el promedio pesaría igual un día de 1 y uno de 2"


def Frontera_de(nombre):
    from apps.fronteras.models import Frontera

    return Frontera.objects.get(nombre_frontera=nombre)


# ── Detalles que ya costaron una cuenta mal ─────────────────────────────────


def test_cgm_cuenta_en_mayuscula_y_en_minuscula(base_limpia):
    """Generación guarda "cgm" y consumo "CGM". Una consulta escrita a mano que
    no lo sepa cuenta de menos -- pasó el 2026-09-15 revisando esto mismo."""
    from apps.energia.models import ReporteEnergiaConsumo

    f = _frontera("F")
    g = _frontera("G")
    _reporte(f, DIA1, fuente="cgm")
    ReporteEnergiaConsumo.objects.create(frontera_id=g.id, fecha=DIA1, caso="CGM")

    assert _serie()["dias"][0]["automaticas"] == 2


def test_una_frontera_con_generacion_y_consumo_el_mismo_dia_cuenta_una_vez(base_limpia):
    """La tasa es de FRONTERAS, no de filas: si no, una frontera con las dos
    mitades pesaría el doble y podría pasar del 100%."""
    from apps.energia.models import ReporteEnergiaConsumo

    f = _frontera("F")
    _reporte(f, DIA1, fuente="cgm")
    ReporteEnergiaConsumo.objects.create(frontera_id=f.id, fecha=DIA1, caso="CGM")

    assert _serie()["dias"][0] == {
        "fecha": DIA1, "automaticas": 1, "fronteras": 1,
        "reportaron": 1, "sin_corrida": False, "tasa": 100.0,
    }


def test_las_otras_fuentes_no_son_automaticas(base_limpia):
    f = _frontera("F")
    _reporte(f, DIA1, fuente="principal")

    assert _serie()["dias"][0]["tasa"] == 0.0


def test_el_rango_al_reves_se_rechaza():
    from api.exceptions import NoProcesable

    with pytest.raises(NoProcesable):
        _serie(desde=DIA3, hasta=DIA1)


def test_un_solo_dia_devuelve_un_solo_punto(base_limpia):
    _frontera("F")

    assert len(_serie(desde=DIA1, hasta=DIA1)["dias"]) == 1


def test_el_denominador_no_se_mueve_al_cargar_filas(base_limpia):
    """El error del primer intento: se usaba `created_at`, así que importar
    fronteras viejas hacía caer la métrica sin que cambiara nada real."""
    from apps.fronteras.models import Frontera

    _frontera("Vieja", registrada=DIA1)
    # Cargada HOY en nuestra tabla, pero registrada en ASIC hace años.
    Frontera.objects.create(
        nombre_frontera="Importada", codigo_frontera="importada", estado="activa",
        fecha_registro_asic=date(2020, 1, 1),
    )

    assert [d["fronteras"] for d in _serie()["dias"]] == [2, 2, 2]


def test_una_frontera_sin_registro_asic_no_entra(base_limpia):
    """No se le puede exigir un reporte a algo que el mercado no conoce."""
    from apps.fronteras.models import Frontera

    _frontera("Registrada")
    Frontera.objects.create(
        nombre_frontera="Sin ASIC", codigo_frontera="sinasic", estado="activa")

    assert [d["fronteras"] for d in _serie()["dias"]] == [1, 1, 1]


# ── El mismo conjunto que arma el reporte ───────────────────────────────────


def test_una_frontera_inactiva_no_entra(base_limpia):
    """`estado` es nuestro control de apagado: una frontera que marcamos
    inactiva no reporta, así que tampoco puede contar como falla."""
    _frontera("Activa")
    _frontera("Apagada", estado="inactiva")

    assert [d["fronteras"] for d in _serie()["dias"]] == [1, 1, 1]


def test_una_frontera_sin_codigo_no_entra(base_limpia):
    """Sin `codigo_frontera` el orquestador no la puede cruzar contra Quoia, así
    que nunca va a reportar."""
    from apps.fronteras.models import Frontera

    _frontera("Con codigo")
    Frontera.objects.create(
        nombre_frontera="Sin codigo", estado="activa", fecha_registro_asic=DIA1)

    assert [d["fronteras"] for d in _serie()["dias"]] == [1, 1, 1]


def test_el_filtro_es_el_mismo_del_orquestador():
    """Si el reporte y esta métrica dejaran de mirar el mismo conjunto, las de
    más serían fallas permanentes que ninguna automatización puede arreglar."""
    import inspect

    from apps.energia.services.reporte import orquestador, vistas

    del_reporte = inspect.getsource(orquestador._fronteras_con_reporte)
    de_la_metrica = inspect.getsource(vistas._fronteras_reportables_por_dia)

    for condicion in ('estado="activa"', "codigo_frontera__isnull=False"):
        assert condicion in del_reporte
        assert condicion in de_la_metrica


# ── Los días sin corrida quedan fuera de la tasa ────────────────────────────
# El 5 y el 6 de septiembre de 2026 no se reportó nada, y fue la MIGRACIÓN DEL
# SERVIDOR (confirmado con la usuaria el 2026-09-15), no la automatización.
# Contarlos movería la métrica por dos causas distintas --cobertura de CGM e
# infraestructura-- sin poder saber cuál fue. Pero siguen apareciendo en la
# serie: esconderlos taparía dos días en que 145 fronteras debían reportar y
# ninguna lo hizo.


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
    assert r["tasa"] == 100.0, "los dos días buenos fueron 100% automáticos"
    assert r["fronteras"] == 8, "el día sin corrida tampoco suma al denominador"


def test_una_corrida_a_medias_tampoco_cuenta(base_limpia):
    """El 5 de septiembre quedó UNA fila de 145. Eso no es una corrida pobre:
    es una corrida que no ocurrió."""
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    for dia in (DIA1, DIA2):
        for f in fronteras:
            _reporte(f, dia)
    _reporte(fronteras[0], DIA3)

    assert _sin_corrida(_serie()) == [DIA3]


def test_una_corrida_casi_completa_si_cuenta(base_limpia):
    """Los días normales cubren entre 95% y 100%. Faltar una frontera es un
    hueco de ESA frontera, no una corrida caída."""
    fronteras = [_frontera(f"F{i}") for i in range(10)]
    for f in fronteras[:9]:
        for dia in (DIA1, DIA2, DIA3):
            _reporte(f, dia)

    assert _sin_corrida(_serie()) == []


def test_el_dia_sin_corrida_sigue_en_la_serie(base_limpia):
    """Sale de la tasa, no de la vista: es la única forma de explicar después
    por qué ese día no hay nada."""
    for i in range(4):
        _frontera(f"F{i}")
    for f in Frontera_todas():
        _reporte(f, DIA1)

    r = _serie()

    assert len(r["dias"]) == 3
    assert r["dias"][2]["fecha"] == DIA3
    assert r["dias"][2]["fronteras"] == 4, "se ve cuántas debían reportar"
    assert r["dias_sin_corrida"] == 2


def Frontera_todas():
    from apps.fronteras.models import Frontera

    return list(Frontera.objects.all())


def test_el_umbral_deja_margen_de_sobra():
    """Los días normales cubren >=95% y los rotos <=1%. El umbral vive en ese
    hueco, así que moverlo no cambia ningún resultado real."""
    from apps.energia.services.reporte.vistas import COBERTURA_MINIMA_DE_UNA_CORRIDA

    assert 0.01 < COBERTURA_MINIMA_DE_UNA_CORRIDA < 0.95
