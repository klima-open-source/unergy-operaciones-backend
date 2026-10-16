"""`apps/proyectos/services/equipos.py`: la vieja info técnica sobre `equipos`.

Desde el 2026-10-15 `proyecto_info_tecnica` se reparte en `proyectos` y `equipos`.
El frontend y la API comercial siguen leyendo y escribiendo con la forma vieja;
lo que se prueba es que esa forma vaya y vuelva sin perder nada, y que los
inversores manden sobre la cantidad escrita a mano.
"""
from decimal import Decimal

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


def _proyecto():
    from apps.proyectos.models import Proyecto

    return Proyecto.objects.create(nombre_comercial="Baraya")


def _ficha(proyecto):
    from apps.proyectos.models import Proyecto
    from apps.proyectos.services.equipos import ficha_tecnica

    return ficha_tecnica(
        Proyecto.objects.prefetch_related("equipos", "inversores").get(pk=proyecto.pk))


def test_sin_nada_tecnico_la_ficha_es_none(base_limpia):
    assert _ficha(_proyecto()) is None


def test_la_ficha_vieja_va_y_vuelve(base_limpia):
    from apps.proyectos.models import Equipo
    from apps.proyectos.services.equipos import guardar_ficha_tecnica

    proyecto = _proyecto()
    avisos = guardar_ficha_tecnica(proyecto, {
        "capacidad_instalada_kwp": Decimal("1339.56"), "voltaje_red": "13.8/800",
        "cantidad_total_paneles": 2016, "potencia_panel_kwp": "0,575",
        "marca_paneles": "Trina", "cantidad_strings": 48, "tipo_tracker": "1P",
        "marca_transformador": "Siemens", "seguridad_fisica": "Cerca y vigilancia",
    })

    assert avisos == []
    f = _ficha(proyecto)
    assert f["capacidad_instalada_kwp"] == 1339.56
    assert f["voltaje_red"] == "13.8/800"
    assert f["cantidad_total_paneles"] == 2016
    assert f["potencia_panel_kwp"] == 0.575
    assert f["marca_paneles"] == "Trina"
    assert f["cantidad_strings"] == 48
    assert f["tipo_tracker"] == "1P"
    assert f["marca_transformador"] == "Siemens"
    assert f["seguridad_fisica"] == "Cerca y vigilancia"
    # Un equipo por tipo: los paneles son UNA fila con su cantidad.
    assert Equipo.objects.get(proyecto=proyecto, tipo="panel").cantidad == 2016
    assert Equipo.objects.get(proyecto=proyecto, tipo="inversor").detalle == "Strings: 48"


def test_una_potencia_de_panel_que_no_es_numero_no_se_guarda(base_limpia):
    from apps.proyectos.models import Equipo
    from apps.proyectos.services.equipos import guardar_ficha_tecnica

    proyecto = _proyecto()
    avisos = guardar_ficha_tecnica(proyecto, {
        "potencia_panel_kwp": "0.565, 0.570", "marca_paneles": "Jinko"})

    assert len(avisos) == 1 and "potencia_panel_kwp" in avisos[0]
    panel = Equipo.objects.get(proyecto=proyecto, tipo="panel")
    assert panel.potencia is None and panel.marca == "Jinko"


def test_vaciar_un_campo_no_crea_equipos(base_limpia):
    from apps.proyectos.models import Equipo
    from apps.proyectos.services.equipos import guardar_ficha_tecnica

    proyecto = _proyecto()
    guardar_ficha_tecnica(proyecto, {"marca_cctv": "", "cctv_estado": None})

    assert not Equipo.objects.filter(proyecto=proyecto).exists()


def test_con_inversores_cargados_manda_su_cantidad(base_limpia):
    from apps.proyectos.models import ProyectoInversor
    from apps.proyectos.services import equipos

    proyecto = _proyecto()
    equipo = equipos.equipo_de_inversores(proyecto.id)
    for n in (1, 2):
        ProyectoInversor.objects.create(proyecto=proyecto, equipo=equipo,
                                        nombre=f"Inversor {n}", potencia_nominal_kw=300)
    ProyectoInversor.objects.create(proyecto=proyecto, equipo=equipo, nombre="Viejo",
                                    potencia_nominal_kw=50, estado="retirado")
    equipos.actualizar_cantidad_inversores(proyecto.id)

    equipos.guardar_ficha_tecnica(proyecto, {"cantidad_inversores": 11,
                                             "marca_inversores": "Huawei"})

    f = _ficha(proyecto)
    # El retirado no cuenta; el 11 escrito a mano no pisa a los cargados.
    assert f["cantidad_inversores"] == 2
    # La marca es del equipo de inversores, no de cada inversor.
    assert f["marca_inversores"] == "Huawei"
    assert equipos.equipo_de_inversores(proyecto.id).marca == "Huawei"


def test_los_campos_que_ya_no_se_guardan_salen_en_null(base_limpia):
    from apps.proyectos.services.equipos import guardar_ficha_tecnica

    proyecto = _proyecto()
    guardar_ficha_tecnica(proyecto, {"marca_cctv": "Hikvision",
                                     "url_ubicacion": "https://maps", "tiene_internet": True})

    f = _ficha(proyecto)
    assert f["marca_cctv"] == "Hikvision"
    assert f["url_ubicacion"] is None and f["tiene_internet"] is None
