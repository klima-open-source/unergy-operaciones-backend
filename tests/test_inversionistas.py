"""`inversionistas`: una fila por cliente que es inversionista de alguna planta.

La participación (`proyecto_inversionistas`) apunta al inversionista; el cliente se
alcanza por él. `cliente` y `cliente_id` siguen como atajos --la API de
inversionistas del proyecto los recibe y devuelve--: al asignarlos, `save()` busca
o crea el inversionista.
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
def datos():
    from django.db import transaction

    from apps.clientes.models import Cliente
    from apps.proyectos.models import Proyecto

    atomica = transaction.atomic()
    atomica.__enter__()
    yield {
        "bia": Cliente.objects.create(razon_social_nombre="Bia Energy S.A.S."),
        "suno": Cliente.objects.create(razon_social_nombre="Suno S.A.S."),
        "planta": Proyecto.objects.create(nombre_comercial="Planta A"),
        "otra": Proyecto.objects.create(nombre_comercial="Planta B"),
    }
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def test_registrar_una_participacion_crea_el_inversionista(datos):
    from apps.proyectos.models import ProyectoInversionista

    part = ProyectoInversionista.objects.create(proyecto=datos["planta"], cliente=datos["bia"])

    assert part.inversionista.cliente_id == datos["bia"].id


def test_el_mismo_cliente_en_dos_plantas_es_un_solo_inversionista(datos):
    from apps.proyectos.models import Inversionista, ProyectoInversionista

    a = ProyectoInversionista.objects.create(proyecto=datos["planta"], cliente=datos["bia"])
    b = ProyectoInversionista.objects.create(proyecto=datos["otra"], cliente=datos["bia"])

    assert a.inversionista_id == b.inversionista_id
    assert Inversionista.objects.filter(cliente=datos["bia"]).count() == 1


def test_cambiar_el_cliente_cambia_el_inversionista(datos):
    from apps.proyectos.models import ProyectoInversionista

    part = ProyectoInversionista.objects.create(proyecto=datos["planta"], cliente=datos["bia"])
    part.cliente = datos["suno"]
    part.save(update_fields=["cliente"])

    part.refresh_from_db()
    assert part.inversionista.cliente_id == datos["suno"].id


def test_el_cliente_se_lee_por_el_inversionista(datos):
    from apps.proyectos.models import ProyectoInversionista

    ProyectoInversionista.objects.create(proyecto=datos["planta"], cliente=datos["bia"])
    part = ProyectoInversionista.objects.select_related("inversionista__cliente").get()

    assert part.cliente_id == datos["bia"].id
    assert part.cliente.razon_social_nombre == "Bia Energy S.A.S."
    assert ProyectoInversionista.objects.filter(inversionista__cliente=datos["bia"]).count() == 1


def test_un_inversionista_no_esta_dos_veces_en_la_misma_planta(datos):
    from django.db import IntegrityError, transaction

    from apps.proyectos.models import ProyectoInversionista

    ProyectoInversionista.objects.create(proyecto=datos["planta"], cliente=datos["bia"])
    with pytest.raises(IntegrityError), transaction.atomic():
        ProyectoInversionista.objects.create(proyecto=datos["planta"], cliente=datos["bia"])
