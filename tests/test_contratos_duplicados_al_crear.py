"""Crear un contrato avisa si ya hay uno vivo que cubre lo mismo.

Producción acumuló el mismo contrato escrito por tres fuentes que no se
reconocían entre sí --MGS Naos 2 tiene tres filas siendo un solo contrato-- y lo
único que existía era un informe de duplicados que se mira DESPUÉS. Nada impedía
crear el cuarto.

Lo delicado de esto no es detectar el duplicado: es **no estorbar los casos
legítimos**, que son varios y reales. La mitad de estas pruebas son eso.

Avisa y no bloquea (`?forzar=true` lo salta), igual que clientes, proyectos y
fronteras. Decidido con Sara el 2026-09-19, incluido que una renovación creada
mientras el anterior sigue vigente también avise.
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


def _crear(datos_usuario, cuerpo, forzar=False):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.contratos_servicio.views import ContratoServicioViewSet

    url = "/api/v1/contratos-servicio" + ("?forzar=true" if forzar else "")
    peticion = APIRequestFactory().post(url, cuerpo, format="json")
    force_authenticate(peticion, user=UsuarioAutenticado(datos_usuario["usuario"]))
    respuesta = ContratoServicioViewSet.as_view({"post": "create"})(peticion)
    respuesta.render()
    return respuesta


def _planta(nombre="Planta QA"):
    from apps.proyectos.models import Proyecto

    return Proyecto.objects.create(nombre_comercial=nombre)


def _contrato(**kw):
    from apps.contratos.models import ContratoServicio

    kw.setdefault("servicio_aplica", "representacion")
    kw.setdefault("estado", "firmado")
    return ContratoServicio.objects.create(**kw)


def _cliente(nombre):
    from apps.clientes.models import Cliente

    return Cliente.objects.create(razon_social_nombre=nombre)


# ── Avisa cuando debe ─────────────────────────────────────────────────────────

def test_avisa_si_la_planta_ya_tiene_ese_servicio_vivo(datos):
    planta = _planta()
    _contrato(proyecto=planta, numero_contrato="UNERGY-RC-001")

    respuesta = _crear(datos, {
        "servicio_aplica": "representacion", "proyecto_id": planta.id,
    })

    assert respuesta.status_code == 409, respuesta.data
    detalle = respuesta.data["detail"]
    assert detalle["duplicado_contrato"] is True
    assert "UNERGY-RC-001" in detalle["candidato_nombre"]


def test_el_cgm_avisa_contra_un_contrato_de_representacion(datos):
    """Los dos viajan en el MISMO contrato: un "contrato de CGM" aparte en una
    planta que ya tiene representación es como nacieron los duplicados."""
    planta = _planta()
    _contrato(proyecto=planta, servicio_aplica="representacion")

    respuesta = _crear(datos, {"servicio_aplica": "cgm", "proyecto_id": planta.id})

    assert respuesta.status_code == 409, respuesta.data


def test_dos_arriendos_en_la_misma_planta_avisan(datos):
    planta = _planta()
    _contrato(proyecto=planta, servicio_aplica="arriendo")

    respuesta = _crear(datos, {"servicio_aplica": "arriendo", "proyecto_id": planta.id})

    assert respuesta.status_code == 409, respuesta.data


def test_avisa_cuando_al_existente_le_falta_el_inversionista(datos):
    """No se puede saber a quién pertenece --el caso exacto de Naos 2--, así que
    se pregunta: es preferible un aviso de más."""
    planta = _planta()
    _contrato(proyecto=planta, inversionista_nombre=None)

    respuesta = _crear(datos, {
        "servicio_aplica": "representacion", "proyecto_id": planta.id,
        "inversionista_nombre": "Fondo Solar Uno",
    })

    assert respuesta.status_code == 409, respuesta.data


def test_una_renovacion_con_el_anterior_vigente_tambien_avisa(datos):
    """Decidido así: el aviso dice algo cierto y cuesta un clic."""
    planta = _planta()
    _contrato(proyecto=planta, estado="en_renovacion")

    respuesta = _crear(datos, {
        "servicio_aplica": "representacion", "proyecto_id": planta.id,
    })

    assert respuesta.status_code == 409, respuesta.data


# ── NO estorba los casos legítimos ────────────────────────────────────────────

def test_una_minigranja_admite_un_contrato_por_inversionista(datos):
    """El caso que más importa no romper: en minigranjas hay un contrato de
    representación POR inversionista, cada uno con su tarifa."""
    planta = _planta()
    uno, dos = _cliente("Fondo Solar Uno"), _cliente("Fondo Solar Dos")
    _contrato(proyecto=planta, inversionista=uno, inversionista_nombre="Fondo Solar Uno")

    respuesta = _crear(datos, {
        "servicio_aplica": "representacion", "proyecto_id": planta.id,
        "inversionista_id": dos.id, "inversionista_nombre": "Fondo Solar Dos",
    })

    assert respuesta.status_code == 201, respuesta.data


def test_mantenimiento_y_arriendo_conviven_sin_avisar(datos):
    """Son del mismo grupo (Operación) pero contratos distintos: comparar por
    grupo avisaría cada vez que una planta suma su segundo servicio."""
    planta = _planta()
    _contrato(proyecto=planta, servicio_aplica="mantenimiento")

    respuesta = _crear(datos, {"servicio_aplica": "arriendo", "proyecto_id": planta.id})

    assert respuesta.status_code == 201, respuesta.data


def test_un_contrato_terminado_no_estorba_a_su_reemplazo(datos):
    planta = _planta()
    _contrato(proyecto=planta, estado="terminado")

    respuesta = _crear(datos, {
        "servicio_aplica": "representacion", "proyecto_id": planta.id,
    })

    assert respuesta.status_code == 201, respuesta.data


def test_un_contrato_vencido_por_fecha_tampoco(datos):
    from datetime import timedelta

    from apps.plataforma.services.fechas import hoy_col

    planta = _planta()
    _contrato(proyecto=planta, fecha_fin=hoy_col() - timedelta(days=1))

    respuesta = _crear(datos, {
        "servicio_aplica": "representacion", "proyecto_id": planta.id,
    })

    assert respuesta.status_code == 201, respuesta.data


def test_otra_planta_no_avisa(datos):
    _contrato(proyecto=_planta("Una"))

    respuesta = _crear(datos, {
        "servicio_aplica": "representacion", "proyecto_id": _planta("Otra").id,
    })

    assert respuesta.status_code == 201, respuesta.data


def test_sin_planta_no_se_compara_nada(datos):
    """Un contrato sin `proyecto_id` no se puede situar, y hay 10 así en
    producción."""
    _contrato(proyecto=None)

    respuesta = _crear(datos, {"servicio_aplica": "representacion"})

    assert respuesta.status_code == 201, respuesta.data


def test_forzar_crea_de_todos_modos(datos):
    planta = _planta()
    _contrato(proyecto=planta)

    respuesta = _crear(
        datos, {"servicio_aplica": "representacion", "proyecto_id": planta.id},
        forzar=True,
    )

    assert respuesta.status_code == 201, respuesta.data
