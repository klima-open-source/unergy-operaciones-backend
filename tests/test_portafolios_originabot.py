"""«Sincronizar con TSF» lleva los portafolios de TSF de la planta a Operaciones.

Lo que no puede fallar:
- el vínculo es por id de TSF, porque TSF repite nombres y aquí son únicos;
- un portafolio de Operaciones con el mismo nombre se vincula, no se duplica;
- crear es opt-in (la interfaz lo confirma);
- solo se saca a la planta de portafolios que vienen de TSF: los creados en
  Operaciones no se tocan.
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

    from apps.plataforma.models import Usuario

    atomica = transaction.atomic()
    atomica.__enter__()
    usuario = Usuario.objects.create(nombre="QA", email="qa@unergy.io", rol="admin", activo=True)
    yield {"usuario": usuario}
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


@pytest.fixture
def planta(datos):
    from apps.proyectos.models import Proyecto

    return Proyecto.objects.create(nombre_comercial="MGS 0001", originabot_minifarm_id=424)


def _de(planta):
    from apps.proyectos.models import PortafolioProyecto

    return sorted(
        PortafolioProyecto.objects.filter(proyecto=planta).values_list("portafolio__nombre", flat=True)
    )


def test_crea_solo_si_se_permite_y_la_agrega(planta):
    from apps.proyectos.models import Portafolio
    from apps.proyectos.services import portafolios_originabot as svc

    tsf = [{"id": 207, "name": "Skandia 1"}]

    sin = svc.aplicar(planta, tsf, crear=False)
    assert sin["crear"] == [] and not Portafolio.objects.exists()

    con = svc.aplicar(planta, tsf, crear=True)
    assert [c["nombre"] for c in con["crear"]] == ["Skandia 1"]
    assert Portafolio.objects.get().originabot_portfolio_id == 207
    assert _de(planta) == ["Skandia 1"]


def test_vincula_el_homonimo_de_operaciones_en_vez_de_duplicarlo(planta):
    from apps.proyectos.models import Portafolio
    from apps.proyectos.services import portafolios_originabot as svc

    propio = Portafolio.objects.create(nombre="SKANDIA 1")

    p = svc.plan(planta, [{"id": 207, "name": "Skandia 1"}])
    assert p["crear"] == [] and p["vincular"][0]["portafolio_id"] == propio.id

    svc.aplicar(planta, [{"id": 207, "name": "Skandia 1"}], crear=False)
    propio.refresh_from_db()
    assert propio.originabot_portfolio_id == 207
    assert _de(planta) == ["SKANDIA 1"]


def test_nombre_repetido_en_tsf_no_choca(planta):
    from apps.proyectos.models import Portafolio
    from apps.proyectos.services import portafolios_originabot as svc

    Portafolio.objects.create(nombre="Exagon 2", originabot_portfolio_id=11)

    svc.aplicar(planta, [{"id": 12, "name": "Exagon 2"}], crear=True)

    assert Portafolio.objects.get(originabot_portfolio_id=12).nombre == "Exagon 2 (TSF 12)"


def test_quita_solo_de_los_que_vienen_de_tsf(planta):
    from apps.proyectos.models import Portafolio, PortafolioProyecto
    from apps.proyectos.services import portafolios_originabot as svc

    de_tsf = Portafolio.objects.create(nombre="Sun Factory 1.1", originabot_portfolio_id=82)
    propio = Portafolio.objects.create(nombre="Informe Gerencia")
    for p in (de_tsf, propio):
        PortafolioProyecto.objects.create(portafolio=p, proyecto=planta)

    cambios = svc.aplicar(planta, [], crear=False)

    assert [q["nombre"] for q in cambios["quitar"]] == ["Sun Factory 1.1"]
    assert _de(planta) == ["Informe Gerencia"]


def test_renombra_lo_que_tsf_renombro(planta):
    from apps.proyectos.models import Portafolio
    from apps.proyectos.services import portafolios_originabot as svc

    p = Portafolio.objects.create(nombre="Sun Factory 2", originabot_portfolio_id=136)

    cambios = svc.aplicar(planta, [{"id": 136, "name": "Sun Factory 2.1"}], crear=False)

    p.refresh_from_db()
    assert p.nombre == "Sun Factory 2.1"
    assert cambios["renombrar"][0]["nombre_anterior"] == "Sun Factory 2"


def test_sin_cambios_lo_dice(planta):
    from apps.proyectos.models import Portafolio, PortafolioProyecto
    from apps.proyectos.services import portafolios_originabot as svc

    p = Portafolio.objects.create(nombre="Skandia 1", originabot_portfolio_id=207)
    PortafolioProyecto.objects.create(portafolio=p, proyecto=planta)

    assert svc.plan(planta, [{"id": 207, "name": "Skandia 1"}])["hay_cambios"] is False
