"""Filtro por frontera en el Resumen histórico del Reporte de Energía.

`resumen_historico(desde, hasta, frontera_id=)` recorta la vista a UNA
frontera. Lo que se fija acá es la parte que no es obvia: **el filtro cambia
los conteos, nunca la decisión de qué días cuentan para la tasa**.

`serie_automatico` saca días de la tasa por tres motivos, y los tres describen
la CORRIDA, no una frontera: el día no trajo ni la mitad del volumen normal,
las dos mitades se separaron, o el día corrió entero sin usar CGM en ninguna
frontera. Ese último existe porque un cero absoluto entre 145 fronteras no
pasa nunca -- pero en UNA frontera un día sin CGM es lo normal. Si la regla se
evaluara sobre lo filtrado, leería "el programa falló" y borraría el día: se
irían justo los días en que la frontera no se automatizó, y la tasa daría casi
100% siempre. Por eso las reglas se evalúan siempre con el día completo.

Decidido con la usuaria el 2026-09-21.
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


from datetime import date, datetime, timezone  # noqa: E402

DIA1, DIA2, DIA3 = date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)


def _momento(d: date):
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=timezone.utc)


def _frontera(nombre):
    from apps.fronteras.models import Frontera

    return Frontera.objects.create(
        nombre_frontera=nombre, codigo_frontera=nombre.lower(), estado="activa",
        fecha_registro_asic=DIA1, deleted_at=None,
    )


def _reporte(frontera, dia, fuente="cgm"):
    from apps.energia.models import ReporteEnergiaGeneracion

    return ReporteEnergiaGeneracion.objects.create(
        frontera_id=frontera.id, fecha=dia, medidor_usado=fuente, caso="1",
    )


def _escenario():
    """Cuatro fronteras y tres días:

        DIA1  F0 por CGM, las otras tres por medidor principal
        DIA2  las cuatro por CGM
        DIA3  las cuatro por medidor principal -- NINGUNA por CGM

    F1 es la frontera que se filtra: nunca sale por CGM el DIA1 (normal), y el
    DIA3 el día entero falló.
    """
    fronteras = [_frontera(f"F{i}") for i in range(4)]
    for f in fronteras:
        _reporte(f, DIA1, fuente="cgm" if f is fronteras[0] else "principal")
        _reporte(f, DIA2, fuente="cgm")
        _reporte(f, DIA3, fuente="principal")
    return fronteras


def _serie(frontera_id=None):
    from apps.energia.services.reporte.vistas import serie_automatico

    return serie_automatico(DIA1, DIA3, frontera_id)


def _historico(frontera_id=None):
    from apps.energia.services.reporte.vistas import resumen_historico

    return resumen_historico(DIA1, DIA3, frontera_id)


# ── Lo que el filtro SÍ cambia: los conteos ─────────────────────────────────


def test_sin_filtro_todo_queda_como_estaba(base_limpia):
    fronteras = _escenario()

    r = _serie()

    assert [d["fronteras"] for d in r["dias"]] == [4, 4, 4]
    # DIA1: 1 de 4 por CGM. DIA2: 4 de 4. DIA3 no cuenta (nadie usó CGM).
    assert r["automaticas"] == 5
    assert r["fronteras"] == 8
    assert r["tasa"] == 62.5
    assert len(r["por_frontera"]) == len(fronteras)


def test_con_filtro_los_conteos_son_solo_de_esa_frontera(base_limpia):
    f1 = _escenario()[1]

    r = _serie(f1.id)

    assert [d["fronteras"] for d in r["dias"]] == [1, 1, 1]
    # DIA1 no salió por CGM, DIA2 sí. DIA3 sigue sin contar.
    assert r["automaticas"] == 1
    assert r["fronteras"] == 2
    assert r["tasa"] == 50.0
    assert [f["frontera_id"] for f in r["por_frontera"]] == [f1.id]


def test_el_filtro_recorta_registradas_y_sin_reportar(base_limpia):
    f1 = _escenario()[1]

    assert [d["registradas"] for d in _serie()["dias"]] == [4, 4, 4]
    assert [d["registradas"] for d in _serie(f1.id)["dias"]] == [1, 1, 1]
    # F1 reportó los tres días, así que no le falta ninguno.
    assert [d["sin_reportar"] for d in _serie(f1.id)["dias"]] == [0, 0, 0]


def test_el_filtro_recorta_la_distribucion_de_fuente(base_limpia):
    f1 = _escenario()[1]

    completo = _historico()
    filtrado = _historico(f1.id)

    assert sum(g["total"] for g in completo["distribucion_fuente_generacion"]) == 12
    assert sum(g["total"] for g in filtrado["distribucion_fuente_generacion"]) == 3
    assert {d["frontera_id"] for d in filtrado["detalle_fuente_generacion"]} == {f1.id}
    assert filtrado["frontera_id"] == f1.id


# ── Lo que el filtro NO cambia: qué días cuentan ────────────────────────────


def test_un_dia_sin_cgm_en_esta_frontera_sigue_contando(base_limpia):
    """El DIA1 la frontera filtrada no salió por CGM, pero otra sí. El día
    corrió bien, así que cuenta -- y cuenta como lo que es: un día en que esta
    frontera no se automatizó. Es el corazón del filtro: si la regla se
    evaluara sobre lo filtrado, este día desaparecería y la tasa daría 100%."""
    f1 = _escenario()[1]

    dia1 = _serie(f1.id)["dias"][0]

    assert dia1["excluido"] is False
    assert dia1["automaticas"] == 0
    assert dia1["tasa"] == 0.0
    # La prueba de que no se borró: el denominador del total lo incluye.
    assert _serie(f1.id)["fronteras"] == 2


def test_un_dia_que_fallo_de_verdad_sigue_excluido_con_filtro(base_limpia):
    """El DIA3 ninguna de las cuatro salió por CGM. Eso es el clasificador
    fallando, y lo sigue siendo mires la frontera que mires."""
    from apps.energia.services.reporte.vistas import MOTIVO_SIN_CGM

    f1 = _escenario()[1]

    for resultado in (_serie(), _serie(f1.id)):
        dia3 = resultado["dias"][2]
        assert dia3["excluido"] is True
        assert dia3["motivo"] == MOTIVO_SIN_CGM

    assert _serie(f1.id)["dias_contados"] == 2


def test_el_filtro_no_mueve_los_dias_contados(base_limpia):
    """Mismo veredicto día por día, con filtro y sin él."""
    f1 = _escenario()[1]

    assert ([d["excluido"] for d in _serie()["dias"]]
            == [d["excluido"] for d in _serie(f1.id)["dias"]])
