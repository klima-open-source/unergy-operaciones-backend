"""El diagnóstico de sole.tech: mide, no se cae, no escribe y no hace login.

Se corre en el servidor contra APIs que pueden estar muertas, así que un
timeout, un 401 o un DNS caído tienen que salir como una línea del reporte y no
como un traceback a mitad de camino.
"""
import httpx
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _cliente(manejador) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(manejador))


def test_una_respuesta_buena_cuenta_lo_que_trae():
    from apps.energia.management.commands.diagnosticar_sole_tech import medir

    with _cliente(lambda r: httpx.Response(200, json={"results": [1, 2, 3]})) as http:
        m, cuerpo = medir(http, "prueba", "GET", "https://api.sole.tech/x/")

    assert m.ok
    assert m.estado == 200
    assert m.nota == "3 elementos"
    assert cuerpo == {"results": [1, 2, 3]}


def test_un_401_es_una_falla_con_su_motivo():
    from apps.energia.management.commands.diagnosticar_sole_tech import medir

    with _cliente(lambda r: httpx.Response(401, text="Invalid token.")) as http:
        m, _ = medir(http, "prueba", "GET", "https://api.sole.tech/x/")

    assert not m.ok
    assert m.estado == 401
    assert "Invalid token" in m.nota


@pytest.mark.parametrize("error, nota", [
    (httpx.ReadTimeout("lento"), "timeout"),
    (httpx.ConnectError("sin DNS"), "ConnectError"),
])
def test_un_timeout_o_una_conexion_caida_no_levantan(error, nota):
    from apps.energia.management.commands.diagnosticar_sole_tech import medir

    def manejador(request):
        raise error

    with _cliente(manejador) as http:
        m, cuerpo = medir(http, "prueba", "GET", "https://data.sole.tech/api/project/")

    assert not m.ok
    assert m.estado is None
    assert m.nota == nota
    assert cuerpo is None


def test_los_ids_vacios_o_raros_se_saltan():
    """`project_id_solarview` y `project_id_solenium` son texto en la base."""
    from apps.energia.management.commands.diagnosticar_sole_tech import ids_enteros

    assert ids_enteros(["12", None, "", "abc", 7]) == [12, 7]


def test_solo_lee():
    """Todo es GET. Un ON/OFF de relay o cualquier otra escritura acá apagaría
    una planta desde un comando de diagnóstico."""
    import inspect

    from apps.energia.management.commands import diagnosticar_sole_tech as modulo

    fuente = inspect.getsource(modulo)

    for verbo in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"', "set-status"):
        assert verbo not in fuente


def test_ya_no_hace_login():
    """Todo sole.tech entra con el token: ni usuario ni contraseña."""
    import inspect

    from apps.energia.management.commands import diagnosticar_sole_tech as modulo

    fuente = inspect.getsource(modulo)

    for resto in ("SOLENIUM_USER", "SOLENIUM_PASS", "auth.sole.tech", "Bearer"):
        assert resto not in fuente


def test_con_los_dos_ids_no_lee_la_base(monkeypatch):
    """En local la base puede no estar: con las dos plantas de muestra dadas,
    el comando tiene que llegar a sole.tech sin tocarla."""
    from io import StringIO

    from django.core.management import call_command

    from apps.energia.management.commands import diagnosticar_sole_tech as modulo
    from apps.proyectos.models import Proyecto

    def sin_base(*a, **k):
        raise AssertionError("no debía leer la base")

    monkeypatch.setattr(Proyecto.objects, "filter", sin_base)
    monkeypatch.setenv("SOLARVIEW_TOKEN", "tok")
    visitadas = []

    def falso(http, que, metodo, url, **kwargs):
        visitadas.append(url)
        return modulo.Medida(que, url, 200, 1), None

    monkeypatch.setattr(modulo, "medir", falso)

    call_command("diagnosticar_sole_tech", "--id-solenium", "5", "--id-solarview", "12",
                 stdout=StringIO())

    assert any("/project/5/inverter/" in u for u in visitadas)
    assert any("/project-detail/12/" in u for u in visitadas)

