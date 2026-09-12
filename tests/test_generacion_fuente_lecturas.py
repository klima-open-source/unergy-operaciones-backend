"""La respuesta dice si la curva salió de lecturas verificadas o crudas.

`verified_by_operator` es un campo de la API de Unergy --no nuestro, y no hay
columna equivalente en ninguna base a la que lleguemos-- que marca las lecturas
que alguien revisó. `lecturas_con_respaldo` pide primero esas y, si la planta no
tiene ninguna, cae a todas. El respaldo está bien: sin él, esas plantas tendrían
la gráfica vacía.

Lo que faltaba era DECIRLO. Medido contra la API el 2026-09-12, sobre 20 plantas
y seis semanas: 19 tenían lecturas verificadas y una (GD Delta 2) ninguna de sus
1.002. Un grupo estaba al 100% y otro cerca del 74%. O sea que dos plantas del
mismo sitio pueden salir una depurada y la otra cruda en el mismo gráfico, y
compararse además contra la misma meta P90, sin nada que lo advierta.

Los tres valores importan y no se pueden colapsar: "sin_datos" no es lo mismo
que "cruda y vacía" -- la primera dice que no hay nada que mostrar, la segunda
que se mostró lo que había sin revisar.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


@pytest.fixture
def api(monkeypatch):
    """La API de Unergy, simulada: `{verificadas: [...], todas: [...]}`."""
    from apps.energia.services import unergy_api

    estado = {"verificadas": [], "todas": [], "llamadas": []}

    def _crudas(token_, sub_project, desde, hasta, solo_verificadas):
        estado["llamadas"].append(solo_verificadas)
        return list(estado["verificadas"] if solo_verificadas else estado["todas"])

    monkeypatch.setattr(unergy_api, "lecturas_crudas", _crudas)
    return estado


def _pedir():
    from apps.energia.services.unergy_api import lecturas_con_respaldo

    return lecturas_con_respaldo("tk", "sub-1", "2026-08-01", "2026-08-31")


def test_con_verificadas_se_usan_esas(api):
    api["verificadas"] = [{"generacion": 1}]
    api["todas"] = [{"generacion": 1}, {"generacion": 2}]

    lecturas, fuente = _pedir()

    assert fuente == "verificada"
    assert len(lecturas) == 1


def test_con_verificadas_no_se_pide_el_resto(api):
    """El respaldo cuesta una llamada externa: no se paga si no hace falta."""
    api["verificadas"] = [{"generacion": 1}]

    _pedir()

    assert api["llamadas"] == [True], "pidió las crudas sin necesitarlas"


def test_sin_verificadas_cae_a_las_crudas_y_lo_dice(api):
    """El caso de GD Delta 2: 1.002 lecturas, ninguna verificada."""
    api["verificadas"] = []
    api["todas"] = [{"generacion": 1}, {"generacion": 2}]

    lecturas, fuente = _pedir()

    assert fuente == "cruda"
    assert len(lecturas) == 2
    assert api["llamadas"] == [True, False]


def test_sin_ninguna_lectura_no_es_cruda(api):
    """"sin_datos" y no "cruda": una dice que no hay nada, la otra que se mostró
    lo que había sin revisar. La gráfica las dibuja distinto."""
    api["verificadas"] = []
    api["todas"] = []

    lecturas, fuente = _pedir()

    assert fuente == "sin_datos"
    assert lecturas == []


def _sin_base(monkeypatch):
    """`build_generation` busca el proyecto para la simulación; acá no interesa
    y la consulta traería la base entera a una prueba que es sobre una etiqueta."""
    from types import SimpleNamespace

    from api.v1.monitoreo import queryset

    falso = SimpleNamespace(
        Proyecto=SimpleNamespace(
            objects=SimpleNamespace(
                filter=lambda **kw: SimpleNamespace(first=lambda: None)
            )
        )
    )
    monkeypatch.setattr(queryset, "py_models", falso)


def test_el_endpoint_manda_la_fuente(monkeypatch):
    """El contrato con el frontend."""
    from api.v1.monitoreo import queryset
    from apps.energia.services import unergy_api

    monkeypatch.setattr(unergy_api, "token", lambda: "tk")
    monkeypatch.setattr(
        unergy_api, "lecturas_con_respaldo",
        lambda *a, **k: ([], "cruda"),
    )
    monkeypatch.setattr(unergy_api, "deltas", lambda *a, **k: [])
    _sin_base(monkeypatch)

    from datetime import date

    respuesta = queryset.build_generation("sub-1", date(2026, 8, 1), date(2026, 8, 31))

    assert respuesta["ok"] is True
    assert respuesta["fuente"] == "cruda"


def test_si_la_api_falla_no_se_inventa_una_fuente(monkeypatch):
    from datetime import date

    from api.v1.monitoreo import queryset
    from apps.energia.services import unergy_api

    monkeypatch.setattr(unergy_api, "token", lambda: "tk")

    def _reventar(*a, **k):
        raise RuntimeError("API caída")

    monkeypatch.setattr(unergy_api, "lecturas_con_respaldo", _reventar)

    respuesta = queryset.build_generation("sub-1", date(2026, 8, 1), date(2026, 8, 31))

    assert respuesta["ok"] is False
    assert "fuente" not in respuesta


def test_los_otros_consumidores_desempaquetan_la_tupla():
    """`lecturas_con_respaldo` pasó a devolver `(lecturas, fuente)`. Los dos que
    no usan la etiqueta tienen que desempaquetarla igual: sin eso le pasarían la
    tupla entera a `deltas`, que la recorrería como si fuera la lista y
    devolvería una curva vacía -- sin error, sin dato."""
    import inspect

    from apps.energia.services import unergy_api
    from apps.proyectos.services import gen_promedio

    for modulo in (unergy_api, gen_promedio):
        fuente = inspect.getsource(modulo)
        assert "lecturas, _fuente = " in fuente, f"{modulo.__name__} no desempaqueta"
