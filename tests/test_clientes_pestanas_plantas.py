"""Las pestañas Proyectos y Fronteras de un cliente, contra el Resumen.

Bug reportado el 2026-09-10: en la ficha de un cliente, el Resumen mostraba
proyectos, contratos y servicios que la pestaña correspondiente no tenia.

La causa: tres definiciones distintas de "las plantas de este cliente"
conviviendo. El Resumen y la pestaña Servicios usan `proyectos_por_cliente`
--inversionista U contratante/prestador de un contrato de servicio U plantas de
sus PPA--, mientras Proyectos y Fronteras usaban SOLO `ProyectoInversionista`
("solo ese rol", decia su docstring). Una planta que llegaba por contrato o por
PPA salia en el Resumen y no en esas dos pestañas; y como Fronteras derivaba de
la definicion estrecha, esa planta tampoco mostraba ninguna frontera.

El criterio amplio es el que esta bien pensado y su razon esta escrita en
`_contratos_del_cliente`: `contratante_id`/`prestador_id` casi nunca se pueblan
--el campo del wizard es texto libre-- asi que sin el camino por planta el panel
quedaba vacio aunque el cliente tuviera contratos reales. El caso que lo motivo
fue Quantum, inversionista de GD Sirius y GD Elektra, cuyos contratos de
representacion no lo tienen como contratante.

Cada planta ahora dice POR QUE esta en la lista (`roles`): sin eso la pestaña
crece y nadie sabe de donde salieron las filas nuevas.
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


def _pedir(datos_usuario, cliente_id, accion):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.clientes.views import ClienteViewSet

    peticion = APIRequestFactory().get(f"/api/v1/clientes/{cliente_id}/{accion}")
    force_authenticate(peticion, user=UsuarioAutenticado(datos_usuario["usuario"]))
    respuesta = ClienteViewSet.as_view({"get": accion})(peticion, pk=cliente_id)
    respuesta.render()
    return respuesta


def _cliente(nombre="Cliente"):
    from apps.clientes.models import Cliente

    return Cliente.objects.create(razon_social_nombre=nombre)


def _proyecto(nombre, **kw):
    from apps.proyectos.models import Proyecto

    return Proyecto.objects.create(nombre_comercial=nombre, **kw)


def _inversionista(cliente, proyecto):
    from apps.proyectos.models import ProyectoInversionista

    return ProyectoInversionista.objects.create(cliente=cliente, proyecto=proyecto)


def _contrato_servicio(proyecto, **kw):
    from apps.contratos.models import ContratoServicio

    kw.setdefault("servicio_aplica", "representacion")
    return ContratoServicio.objects.create(proyecto=proyecto, **kw)


def _ppa_con_planta(cliente, proyecto, como="comprador"):
    from apps.ppa.models import PpaContrato, PpaContratoProyecto

    contrato = PpaContrato.objects.create(**{f"{como}_id": cliente.id})
    PpaContratoProyecto.objects.create(contrato=contrato, proyecto=proyecto)
    return contrato


def _frontera(proyecto, codigo):
    from apps.fronteras.models import Frontera

    return Frontera.objects.create(
        proyecto=proyecto, codigo_frontera=codigo,
        nombre_frontera=f"Frontera {codigo}", tipo_frontera="generacion",
    )


def _nombres(respuesta):
    return {p["nombre_comercial"] for p in respuesta.data}


# ── El criterio de "sus plantas" ─────────────────────────────────────────────

def test_incluye_la_planta_donde_es_inversionista(datos):
    cliente = _cliente("Quantum")
    _inversionista(cliente, _proyecto("GD Sirius"))

    respuesta = _pedir(datos, cliente.id, "proyectos")

    assert respuesta.status_code == 200, respuesta.data
    assert _nombres(respuesta) == {"GD Sirius"}


def test_incluye_la_planta_donde_es_contratante_sin_ser_inversionista(datos):
    """El caso que faltaba: el cliente firma el contrato de esa planta pero no
    tiene participacion en ella. Salia en el Resumen y no en esta pestaña."""
    cliente = _cliente("Contratante Puro")
    proyecto = _proyecto("GD La María")
    _contrato_servicio(proyecto, contratante_id=cliente.id)

    respuesta = _pedir(datos, cliente.id, "proyectos")

    assert _nombres(respuesta) == {"GD La María"}
    assert respuesta.data[0]["roles"] == ["contratante"]


def test_incluye_la_planta_que_cubre_uno_de_sus_ppa(datos):
    cliente = _cliente("Comprador PPA")
    proyecto = _proyecto("Acanto")
    _ppa_con_planta(cliente, proyecto)

    respuesta = _pedir(datos, cliente.id, "proyectos")

    assert _nombres(respuesta) == {"Acanto"}
    assert respuesta.data[0]["roles"] == ["ppa"]


def test_no_incluye_plantas_ajenas(datos):
    cliente = _cliente("Con Una Planta")
    _inversionista(cliente, _proyecto("La Suya"))
    _proyecto("La De Otro")

    respuesta = _pedir(datos, cliente.id, "proyectos")

    assert _nombres(respuesta) == {"La Suya"}


def test_un_proyecto_borrado_no_aparece(datos):
    from django.utils import timezone

    cliente = _cliente("Con Planta Borrada")
    _inversionista(cliente, _proyecto("Borrada", deleted_at=timezone.now()))

    respuesta = _pedir(datos, cliente.id, "proyectos")

    assert respuesta.data == []


# ── Los roles ────────────────────────────────────────────────────────────────

def test_una_planta_puede_tener_varios_roles(datos):
    """Inversionista Y contratante de la misma planta: los dos, sin repetir."""
    cliente = _cliente("Doble Rol")
    proyecto = _proyecto("GD Elektra")
    _inversionista(cliente, proyecto)
    _contrato_servicio(proyecto, contratante_id=cliente.id)
    # Un segundo contrato del mismo cliente en la misma planta no duplica el rol.
    _contrato_servicio(proyecto, contratante_id=cliente.id, servicio_aplica="cgm")

    respuesta = _pedir(datos, cliente.id, "proyectos")

    assert respuesta.data[0]["roles"] == ["inversionista", "contratante"]


def test_el_prestador_tambien_es_un_rol(datos):
    """Un cliente que PRESTA el servicio tiene esa planta "con nosotros" -- el
    mismo criterio que usa `proyectos_por_cliente`."""
    cliente = _cliente("Prestador")
    proyecto = _proyecto("Planta Prestada")
    _contrato_servicio(proyecto, prestador_id=cliente.id)

    respuesta = _pedir(datos, cliente.id, "proyectos")

    assert respuesta.data[0]["roles"] == ["prestador"]


def test_un_ppa_borrado_no_pone_el_rol_ni_la_planta(datos):
    from django.utils import timezone

    from apps.ppa.models import PpaContrato

    cliente = _cliente("PPA Borrado")
    proyecto = _proyecto("Planta Del PPA Borrado")
    contrato = _ppa_con_planta(cliente, proyecto)
    PpaContrato.objects.filter(pk=contrato.pk).update(deleted_at=timezone.now())

    respuesta = _pedir(datos, cliente.id, "proyectos")

    assert respuesta.data == []


# ── La potencia ──────────────────────────────────────────────────────────────

def test_la_potencia_viaja_como_potencia_ac_kw(datos):
    """El front leia `potencia_instalada_kwp` --el nombre de la columna-- y lo
    que llegaba se llamaba `potencia_kwp`: la potencia no se mostraba en ninguna
    planta. El nombre nuevo dice lo que el dato es (AC), aunque la columna siga
    llamandose `..._kwp`."""
    cliente = _cliente("Con Potencia")
    _inversionista(cliente, _proyecto("Con Potencia", potencia_instalada_kwp=1500))

    respuesta = _pedir(datos, cliente.id, "proyectos")

    assert respuesta.data[0]["potencia_ac_kw"] == 1500.0


# ── Fronteras ────────────────────────────────────────────────────────────────

def test_las_fronteras_siguen_el_mismo_criterio_que_los_proyectos(datos):
    """Una planta que llega por contrato mostraba CERO fronteras, porque esta
    pestaña derivaba de la definicion estrecha."""
    cliente = _cliente("Contratante Con Fronteras")
    proyecto = _proyecto("Planta Contratada")
    _contrato_servicio(proyecto, contratante_id=cliente.id)
    _frontera(proyecto, "frt00001")

    respuesta = _pedir(datos, cliente.id, "fronteras")

    assert respuesta.status_code == 200, respuesta.data
    assert [f["codigo_frontera"] for f in respuesta.data] == ["frt00001"]


def test_las_fronteras_de_la_planta_del_inversionista_siguen_saliendo(datos):
    cliente = _cliente("Inversionista Con Fronteras")
    proyecto = _proyecto("Planta Propia")
    _inversionista(cliente, proyecto)
    _frontera(proyecto, "frt00002")
    _frontera(proyecto, "frt00003")

    respuesta = _pedir(datos, cliente.id, "fronteras")

    assert [f["codigo_frontera"] for f in respuesta.data] == ["frt00002", "frt00003"]


def test_un_cliente_sin_plantas_no_devuelve_nada(datos):
    cliente = _cliente("Sin Nada")

    assert _pedir(datos, cliente.id, "proyectos").data == []
    assert _pedir(datos, cliente.id, "fronteras").data == []


# ── Coherencia con el Resumen, que es el punto de todo esto ──────────────────

def test_las_plantas_de_la_pestana_son_las_del_resumen(datos):
    """La prueba que fija la propiedad, no la implementacion: las dos vistas
    tienen que responder lo mismo, venga la planta por donde venga."""
    from apps.clientes.services.vistas import panel_360

    cliente = _cliente("Con Los Tres Caminos")
    propia = _proyecto("Por Participacion")
    contratada = _proyecto("Por Contrato")
    del_ppa = _proyecto("Por PPA")
    _inversionista(cliente, propia)
    _contrato_servicio(contratada, contratante_id=cliente.id)
    _ppa_con_planta(cliente, del_ppa)

    pestana = _nombres(_pedir(datos, cliente.id, "proyectos"))
    resumen = {p["nombre"] for p in panel_360(cliente)["plantas"]}

    assert pestana == resumen
    assert pestana == {"Por Participacion", "Por Contrato", "Por PPA"}
