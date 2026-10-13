"""`contrato_proyectos` para todos los contratos, con la participación en representación.

Los contratos de servicio guardan su planta en `contratos.proyecto_id`, y
`Contrato.save()` deriva de ahí su fila en `contrato_proyectos`. La participación
(a qué inversionista de la planta representamos) solo va en representación y nunca
se adivina entre varias (`apps/contratos/services/contrato_proyectos.py`).
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
    from apps.proyectos.models import Proyecto, ProyectoInversionista

    atomica = transaction.atomic()
    atomica.__enter__()
    bia = Cliente.objects.create(razon_social_nombre="Bia Energy S.A.S.")
    suno = Cliente.objects.create(razon_social_nombre="Suno S.A.S.")
    compartida = Proyecto.objects.create(nombre_comercial="Planta compartida")
    sola = Proyecto.objects.create(nombre_comercial="Planta de Bia")
    yield {
        "bia": bia, "suno": suno, "compartida": compartida, "sola": sola,
        "vacia": Proyecto.objects.create(nombre_comercial="Planta sin inversionistas"),
        "bia_en_compartida": ProyectoInversionista.objects.create(proyecto=compartida, cliente=bia),
        "suno_en_compartida": ProyectoInversionista.objects.create(proyecto=compartida, cliente=suno),
        "bia_en_sola": ProyectoInversionista.objects.create(proyecto=sola, cliente=bia),
    }
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _contrato(servicio, proyecto, inversionista=None):
    from apps.contratos.models import ContratoServicio

    return ContratoServicio.objects.create(
        servicio_aplica=servicio, proyecto=proyecto, inversionista=inversionista,
    )


def _fila(contrato):
    from apps.contratos.models import ContratoProyecto

    return ContratoProyecto.objects.get(contrato=contrato)


def test_representacion_con_inversionista_toma_su_participacion(datos):
    contrato = _contrato("representacion", datos["compartida"], datos["suno"])

    fila = _fila(contrato)
    assert fila.proyecto_id == datos["compartida"].id
    assert fila.proyecto_inversionista_id == datos["suno_en_compartida"].id


def test_sin_inversionista_y_una_sola_participacion_toma_esa(datos):
    contrato = _contrato("representacion", datos["sola"])

    assert _fila(contrato).proyecto_inversionista_id == datos["bia_en_sola"].id


def test_sin_inversionista_y_varias_participaciones_no_adivina(datos):
    contrato = _contrato("representacion", datos["compartida"])

    assert _fila(contrato).proyecto_inversionista_id is None


def test_un_contrato_que_no_es_de_representacion_no_lleva_participacion(datos):
    contrato = _contrato("mantenimiento", datos["sola"], datos["bia"])

    fila = _fila(contrato)
    assert fila.proyecto_id == datos["sola"].id
    assert fila.proyecto_inversionista_id is None


def test_cambiar_la_planta_mueve_la_fila(datos):
    contrato = _contrato("representacion", datos["compartida"], datos["bia"])
    contrato.proyecto = datos["sola"]
    contrato.save()

    fila = _fila(contrato)
    assert (fila.proyecto_id, fila.proyecto_inversionista_id) == (datos["sola"].id, datos["bia_en_sola"].id)


def test_quitar_la_planta_quita_la_fila(datos):
    from apps.contratos.models import ContratoProyecto

    contrato = _contrato("mantenimiento", datos["sola"])
    contrato.proyecto = None
    contrato.save()

    assert not ContratoProyecto.objects.filter(contrato=contrato).exists()


def test_una_participacion_asignada_a_mano_no_se_pierde_al_guardar(datos):
    contrato = _contrato("representacion", datos["compartida"])
    fila = _fila(contrato)
    fila.proyecto_inversionista = datos["suno_en_compartida"]
    fila.save()
    contrato.save()

    assert _fila(contrato).proyecto_inversionista_id == datos["suno_en_compartida"].id


def test_borrar_la_participacion_deja_el_contrato_pendiente(datos):
    contrato = _contrato("representacion", datos["compartida"], datos["suno"])
    datos["suno_en_compartida"].delete()

    assert _fila(contrato).proyecto_inversionista_id is None


def test_los_atajos_leen_la_planta_y_el_inversionista(datos):
    from apps.contratos.models import ContratoServicio
    from apps.contratos.services.contrato_proyectos import CON_PLANTA

    _contrato("representacion", datos["compartida"], datos["suno"])
    contrato = ContratoServicio.objects.prefetch_related(CON_PLANTA).get()

    assert contrato.proyecto_id == datos["compartida"].id
    assert contrato.proyecto.nombre_comercial == "Planta compartida"
    assert contrato.inversionista_id == datos["suno"].id
    assert contrato.inversionista_nombre == "Suno S.A.S."


def test_se_filtra_por_planta_y_por_inversionista(datos):
    from apps.contratos.models import ContratoServicio

    a = _contrato("representacion", datos["compartida"], datos["suno"])
    _contrato("mantenimiento", datos["sola"])

    assert list(ContratoServicio.objects.filter(
        proyectos_vinculados__proyecto=datos["compartida"])) == [a]
    assert list(ContratoServicio.objects.filter(
        proyectos_vinculados__proyecto_inversionista__inversionista__cliente=datos["suno"])) == [a]


def test_un_inversionista_que_no_participa_en_la_planta_se_rechaza(datos):
    from apps.contratos.services.contrato_proyectos import InversionistaFueraDeLaPlanta

    with pytest.raises(InversionistaFueraDeLaPlanta):
        _contrato("representacion", datos["sola"], datos["suno"])


def test_fusionar_clientes_que_comparten_planta_conserva_al_representado(datos, monkeypatch):
    from apps.clientes.services import gestion

    monkeypatch.setattr(gestion, "registrar_borrado", lambda *a, **kw: None)
    from django.db import connection

    connection.connection.create_function("NOW", 0, lambda: "2026-10-13 00:00:00")
    contrato = _contrato("representacion", datos["compartida"], datos["suno"])

    movimientos, copiados = gestion.reporte_merge(datos["bia"], datos["suno"])
    gestion.ejecutar_merge(datos["bia"], datos["suno"], movimientos, copiados)

    assert _fila(contrato).proyecto_inversionista_id == datos["bia_en_compartida"].id


def test_fusionar_proyectos_con_el_mismo_inversionista_conserva_al_representado(datos, monkeypatch):
    from apps.proyectos.models import ProyectoInversionista
    from apps.proyectos.services import gestion

    monkeypatch.setattr(gestion, "registrar_borrado", lambda *a, **kw: None)
    contrato = _contrato("representacion", datos["sola"], datos["bia"])

    movimientos, copiados = gestion.reporte_merge(datos["compartida"], datos["sola"])
    gestion.ejecutar_merge(datos["compartida"], datos["sola"], movimientos, copiados)

    fila = _fila(contrato)
    assert fila.proyecto_id == datos["compartida"].id
    assert fila.proyecto_inversionista_id == datos["bia_en_compartida"].id
    assert ProyectoInversionista.objects.filter(proyecto=datos["compartida"]).count() == 2


def test_registrar_el_primer_inversionista_completa_el_contrato_pendiente(datos):
    """El barrido carga los inversionistas planta por planta: el contrato que
    esperaba no queda pendiente hasta que alguien lo vuelva a guardar."""
    from apps.proyectos.models import ProyectoInversionista

    contrato = _contrato("representacion", datos["vacia"])
    assert _fila(contrato).proyecto_inversionista_id is None

    nueva = ProyectoInversionista.objects.create(proyecto=datos["vacia"], cliente=datos["bia"])

    assert _fila(contrato).proyecto_inversionista_id == nueva.id


def test_un_segundo_inversionista_no_decide_por_nadie(datos):
    from apps.proyectos.models import ProyectoInversionista

    contrato = _contrato("representacion", datos["compartida"])
    ProyectoInversionista.objects.create(
        proyecto=datos["compartida"], cliente=_otro_cliente("Tercer fondo"))

    assert _fila(contrato).proyecto_inversionista_id is None


def test_el_que_queda_cuando_otro_sale_no_hereda_su_contrato(datos):
    """Si Suno sale de la planta y solo queda Bia, el contrato de Suno no pasa a Bia."""
    from apps.proyectos.models import ProyectoInversionista

    contrato = _contrato("representacion", datos["compartida"], datos["suno"])
    datos["suno_en_compartida"].delete()
    bia = ProyectoInversionista.objects.get(pk=datos["bia_en_compartida"].pk)
    bia.porcentaje_participacion = 1
    bia.save()

    assert _fila(contrato).proyecto_inversionista_id is None


def _otro_cliente(nombre):
    from apps.clientes.models import Cliente

    return Cliente.objects.create(razon_social_nombre=nombre)
