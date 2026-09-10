"""`manage.py normalizar_nit_clientes`: el backfill del NIT.

El serializer ya guarda el NIT con solo sus digitos, pero eso solo arregla lo
que se escriba de ahora en adelante: mientras las filas viejas conserven su
formato ("900.123.456-7"), un alta nueva no choca con ellas y la proteccion no
sirve para lo que YA esta. Este comando las normaliza.

Lo que se fija aca es sobre todo la prudencia del comando: que no escriba si no
se lo piden, y que las colisiones --dos clientes cuyos NIT normalizan al mismo
valor, que casi seguro son el mismo cliente cargado dos veces-- las REPORTE y
las deje intactas en vez de intentar arreglarlas. Normalizarlas es imposible (el
UNIQUE lo impide) y decidir cual sobrevive no es cosa de un comando.
"""
from io import StringIO

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


def _cliente(nombre, nit=None, **kw):
    from apps.clientes.models import Cliente

    return Cliente.objects.create(razon_social_nombre=nombre, nit_cedula=nit, **kw)


def _correr(*args):
    from django.core.management import call_command

    salida = StringIO()
    call_command("normalizar_nit_clientes", *args, stdout=salida, stderr=salida)
    return salida.getvalue()


def _nit(cliente_id):
    from apps.clientes.models import Cliente

    return Cliente.objects.get(pk=cliente_id).nit_cedula


def test_por_defecto_reporta_y_no_escribe(base_limpia):
    cliente = _cliente("Con Puntos", "900.123.456-7")

    salida = _correr()

    assert "900.123.456-7" in salida and "9001234567" in salida
    assert _nit(cliente.id) == "900.123.456-7", "no debia escribir sin --ejecutar"
    assert "Nada escrito" in salida


def test_con_ejecutar_normaliza(base_limpia):
    uno = _cliente("Con Puntos", "900.123.456-7")
    dos = _cliente("Con Espacios", " 800 234 567 ")

    _correr("--ejecutar")

    assert _nit(uno.id) == "9001234567"
    assert _nit(dos.id) == "800234567"


def test_no_toca_lo_que_ya_esta_normalizado(base_limpia):
    cliente = _cliente("Ya Limpio", "9001234567")

    salida = _correr("--ejecutar")

    assert _nit(cliente.id) == "9001234567"
    assert "Por normalizar:   0" in salida


def test_las_colisiones_se_reportan_y_se_dejan_intactas(base_limpia):
    """Dos formas del mismo NIT: normalizar las dos es imposible (UNIQUE), y
    elegir cual sobrevive es una decision de negocio."""
    uno = _cliente("Duplicado A", "900.123.456-7")
    dos = _cliente("Duplicado B", "900123456-7")

    salida = _correr("--ejecutar")

    assert _nit(uno.id) == "900.123.456-7"
    assert _nit(dos.id) == "900123456-7"
    assert "Colisiones:       1 grupo(s)" in salida
    assert "Duplicado A" in salida and "Duplicado B" in salida


def test_una_colision_no_bloquea_al_resto(base_limpia):
    """El grupo que choca se salta; los demas se normalizan igual."""
    uno = _cliente("Duplicado A", "900.123.456-7")
    dos = _cliente("Duplicado B", "900123456-7")
    sano = _cliente("Sano", "800.234.567-1")

    _correr("--ejecutar")

    assert _nit(uno.id) == "900.123.456-7"
    assert _nit(dos.id) == "900123456-7"
    assert _nit(sano.id) == "8002345671"


def test_incluye_los_borrados(base_limpia):
    """Un cliente con soft-delete sigue ocupando su NIT en el UNIQUE: si no se
    normaliza, un alta nueva puede chocar contra una fila que nadie ve en el
    listado."""
    from django.utils import timezone

    borrado = _cliente("Borrado", "900.999.888-7", deleted_at=timezone.now())

    _correr("--ejecutar")

    assert _nit(borrado.id) == "9009998887"


def test_los_clientes_sin_nit_no_aparecen(base_limpia):
    _cliente("Sin NIT")
    _cliente("NIT Vacio", "")

    salida = _correr("--ejecutar")

    assert "Clientes con NIT: 0" in salida
