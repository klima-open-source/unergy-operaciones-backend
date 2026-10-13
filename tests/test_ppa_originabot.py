"""PPAs de Operaciones unidos con los de originabot (`apps/ppa/services/originabot_sync.py`).

Contra un originabot falso en memoria: lo que se prueba es la regla, no la red.

- solo se lleva el CAMBIO: lo que originabot tenga y Operaciones no conozca no se toca;
- sin token de Unergy el cambio se acumula y llega entero en el próximo intento;
- un PPA sin vínculo se crea allá con todas sus plantas, si hay off taker;
- vincular a mano a uno existente solo AGREGA;
- una planta sin minigranja no se lleva y se avisa.
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


class OriginabotFalso:
    def __init__(self):
        self.ppas = {10: {"id": 10, "name": "Terpel #1", "minifarms": [1, 99]}}
        # Nombre en originabot → id. La 3 está renombrada, como las antiguas.
        self.minigranjas = {
            "COLX1P1_UNO": 1, "COLX2P1_DOS": 2, "Minigranja 0003 - Tres (COLX3P1)": 3,
            "COLX5P1_A": 5, "COLX5P1_B": 6,
        }
        self.catalogo_offtakers = [{"id": 1, "nombre": "Terpel"}, {"id": 3, "nombre": "NEU"}]
        self.parches = []
        self.creados = []
        self.falla = None

    def _quizas_fallar(self):
        if self.falla:
            raise self.falla

    def ppa(self, ppa_id):
        self._quizas_fallar()
        from apps.comun.integraciones.originabot import OriginabotError

        if ppa_id not in self.ppas:
            raise OriginabotError(f"originabot no encontró el PPA {ppa_id}.")
        return self.ppas[ppa_id]

    def fijar_minifarms(self, ppa_id, minifarms):
        self._quizas_fallar()
        self.parches.append((ppa_id, minifarms))
        self.ppas[ppa_id]["minifarms"] = minifarms

    def crear_ppa(self, nombre, off_taker, minifarms):
        self._quizas_fallar()
        nuevo = {"id": 50 + len(self.creados), "name": nombre, "off_taker": off_taker, "minifarms": minifarms}
        self.creados.append(nuevo)
        self.ppas[nuevo["id"]] = nuevo
        return nuevo

    def offtakers(self):
        return self.catalogo_offtakers

    def buscar_minifarms(self, texto):
        self._quizas_fallar()
        return [{"id": i, "name": n} for n, i in self.minigranjas.items() if texto in n]


@pytest.fixture(autouse=True)
def _limpio():
    """Antes Y después: la base en memoria la comparten los módulos de la suite,
    y lo que quede aquí aparece en los conteos de otros tests."""
    from apps.clientes.models import Cliente
    from apps.contratos.models import Contrato
    from apps.proyectos.models import Proyecto

    def limpiar():
        Contrato.objects.all().delete()
        Cliente.objects.all().delete()
        Proyecto.objects.all().delete()

    limpiar()
    yield
    limpiar()


@pytest.fixture
def ob(monkeypatch):
    from apps.ppa.services import originabot_sync

    falso = OriginabotFalso()
    monkeypatch.setattr(originabot_sync, "ClienteOriginabot", lambda token: falso)
    return falso


@pytest.fixture
def plantas():
    from apps.proyectos.models import Proyecto

    return {
        n: Proyecto.objects.create(nombre_comercial=n.title(), origina_code=f"COLX{i}P1_{n}")
        for i, n in ((1, "UNO"), (2, "DOS"), (3, "TRES"), (5, "CINCO"))
    } | {"SINTSF": Proyecto.objects.create(nombre_comercial="Planta propia")}


def _ppa(plantas_iniciales, **campos):
    from apps.contratos.models import Contrato, ContratoProyecto

    contrato = Contrato.objects.create(grupo="ppa", nombre_interno="Terpel 1", **campos)
    for p in plantas_iniciales:
        ContratoProyecto.objects.create(contrato=contrato, proyecto=p)
    return contrato


def _cambiar(contrato, nuevas):
    from apps.contratos.models import ContratoProyecto
    from apps.ppa.services import originabot_sync

    antes = originabot_sync.plantas_de(contrato)
    ContratoProyecto.objects.filter(contrato=contrato).delete()
    for p in nuevas:
        ContratoProyecto.objects.create(contrato=contrato, proyecto=p)
    originabot_sync.registrar_cambio(contrato, antes, originabot_sync.plantas_de(contrato))


def test_lleva_solo_el_cambio_y_respeta_lo_que_solo_esta_alla(ob, plantas):
    from apps.ppa.services import originabot_sync

    contrato = _ppa([plantas["UNO"]], originabot_ppa_id=10)
    _cambiar(contrato, [plantas["DOS"]])  # sale UNO (1), entra DOS (2)
    originabot_sync.sincronizar(contrato, "tok")

    # 99 es una minigranja que Operaciones no conoce: sigue allá.
    assert ob.ppas[10]["minifarms"] == [2, 99]
    contrato.refresh_from_db()
    assert contrato.originabot_sync_estado == "sincronizado"
    assert contrato.originabot_pendiente is None
    # La minigranja resuelta por nombre queda guardada en la planta.
    plantas["DOS"].refresh_from_db()
    assert plantas["DOS"].originabot_minifarm_id == 2


def test_sin_token_se_acumula_y_llega_entero_despues(ob, plantas):
    from apps.ppa.services import originabot_sync

    contrato = _ppa([plantas["UNO"]], originabot_ppa_id=10)
    _cambiar(contrato, [plantas["UNO"], plantas["DOS"]])
    originabot_sync.sincronizar(contrato, None)
    _cambiar(contrato, [plantas["DOS"], plantas["TRES"]])
    originabot_sync.sincronizar(contrato, None)

    contrato.refresh_from_db()
    assert contrato.originabot_sync_estado == "pendiente"
    assert contrato.originabot_pendiente == {
        "agregar": sorted([plantas["DOS"].id, plantas["TRES"].id]),
        "quitar": [plantas["UNO"].id],
    }
    assert ob.parches == []

    originabot_sync.sincronizar(contrato, "tok")
    assert ob.ppas[10]["minifarms"] == [2, 3, 99]


def test_una_planta_que_entra_y_sale_antes_de_sincronizar_no_viaja(ob, plantas):
    from apps.ppa.services import originabot_sync

    contrato = _ppa([], originabot_ppa_id=10)
    _cambiar(contrato, [plantas["DOS"]])
    _cambiar(contrato, [])
    contrato.refresh_from_db()
    assert contrato.originabot_pendiente is None
    originabot_sync.sincronizar(contrato, "tok")
    assert ob.parches == []


def test_una_planta_que_sale_y_vuelve_antes_de_sincronizar_no_viaja(ob, plantas):
    from apps.ppa.services import originabot_sync

    contrato = _ppa([plantas["UNO"]], originabot_ppa_id=10)
    _cambiar(contrato, [])
    _cambiar(contrato, [plantas["UNO"]])
    contrato.refresh_from_db()
    assert contrato.originabot_pendiente is None
    originabot_sync.sincronizar(contrato, "tok")
    assert ob.ppas[10]["minifarms"] == [1, 99]


def test_minigranja_renombrada_se_encuentra_por_su_codigo_unico(ob, plantas):
    from apps.ppa.services import originabot_sync

    contrato = _ppa([], originabot_ppa_id=10)
    _cambiar(contrato, [plantas["TRES"]])
    originabot_sync.sincronizar(contrato, "tok")

    plantas["TRES"].refresh_from_db()
    assert plantas["TRES"].originabot_minifarm_id == 3
    assert ob.ppas[10]["minifarms"] == [1, 3, 99]


def test_codigo_con_varias_minigranjas_no_se_adivina(ob, plantas):
    from apps.ppa.services import originabot_sync

    contrato = _ppa([], originabot_ppa_id=10)
    _cambiar(contrato, [plantas["CINCO"]])  # COLX5P1 → dos candidatas
    originabot_sync.sincronizar(contrato, "tok")

    contrato.refresh_from_db()
    assert ob.parches == []
    assert "Cinco" in contrato.originabot_sync_detalle


def test_planta_sin_minigranja_no_se_lleva_y_se_avisa(ob, plantas):
    from apps.ppa.services import originabot_sync

    contrato = _ppa([], originabot_ppa_id=10)
    _cambiar(contrato, [plantas["TRES"], plantas["SINTSF"]])
    originabot_sync.sincronizar(contrato, "tok")

    contrato.refresh_from_db()
    assert ob.ppas[10]["minifarms"] == [1, 3, 99]
    assert contrato.originabot_sync_estado == "sincronizado"
    assert "Planta propia" in contrato.originabot_sync_detalle


def test_ppa_sin_vinculo_se_crea_con_todas_sus_plantas(ob, plantas):
    from apps.ppa.services import originabot_sync

    contrato = _ppa([plantas["UNO"], plantas["TRES"]])
    originabot_sync.sincronizar(contrato, "tok", off_taker_id=1, crear=True)

    contrato.refresh_from_db()
    assert ob.creados == [{"id": 50, "name": "Terpel 1", "off_taker": 1, "minifarms": [1, 3]}]
    assert contrato.originabot_ppa_id == 50
    assert contrato.originabot_sync_estado == "sincronizado"


def test_sin_off_taker_queda_pendiente_y_no_crea_nada(ob, plantas):
    from apps.ppa.services import originabot_sync

    contrato = _ppa([plantas["UNO"]])
    originabot_sync.sincronizar(contrato, "tok", crear=True)

    contrato.refresh_from_db()
    assert ob.creados == []
    assert contrato.originabot_ppa_id is None
    assert contrato.originabot_sync_estado == "pendiente"
    assert contrato.originabot_sync_detalle == originabot_sync.SIN_OFFTAKER


def test_nunca_crea_en_originabot_sin_confirmacion_explicita(ob, plantas):
    """Asignar plantas a un PPA sin vínculo NO lo crea allá, aunque el off taker
    se pudiera resolver: crear exige `crear=True`, que la ficha manda solo tras
    el modal de confirmación."""
    from apps.ppa.services import originabot_sync

    contrato = _ppa([plantas["UNO"]])
    originabot_sync.sincronizar(contrato, "tok", off_taker_id=1)

    contrato.refresh_from_db()
    assert ob.creados == []
    assert contrato.originabot_ppa_id is None
    assert contrato.originabot_sync_estado == "pendiente"
    assert contrato.originabot_sync_detalle == originabot_sync.SIN_VINCULO


def test_el_off_taker_sale_del_comprador_por_nombre_y_se_recuerda(ob, plantas):
    from apps.clientes.models import Cliente
    from apps.ppa.services import originabot_sync

    terpel = Cliente.objects.create(razon_social_nombre="ORGANIZACIÓN TERPEL S.A.")
    contrato = _ppa([plantas["UNO"]])
    contrato.comprador_id = terpel.id
    contrato.save()
    originabot_sync.sincronizar(contrato, "tok", crear=True)

    assert ob.creados[0]["off_taker"] == 1
    terpel.refresh_from_db()
    assert terpel.originabot_offtaker_id == 1


def test_vincular_a_uno_existente_solo_agrega(ob, plantas):
    from apps.ppa.services import originabot_sync

    contrato = _ppa([plantas["TRES"]])
    originabot_sync.sincronizar(contrato, "tok", vincular_a=10)

    contrato.refresh_from_db()
    assert contrato.originabot_ppa_id == 10
    assert ob.ppas[10]["minifarms"] == [1, 3, 99]


def test_no_se_vinculan_dos_contratos_al_mismo_ppa(ob, plantas):
    from apps.ppa.services import originabot_sync

    _ppa([], originabot_ppa_id=10)
    otro = _ppa([plantas["UNO"]])
    originabot_sync.sincronizar(otro, "tok", vincular_a=10)

    otro.refresh_from_db()
    assert otro.originabot_ppa_id is None
    assert otro.originabot_sync_estado == "error"


def test_si_originabot_falla_queda_error_y_el_cambio_espera(ob, plantas):
    from apps.comun.integraciones.originabot import OriginabotError
    from apps.ppa.services import originabot_sync

    contrato = _ppa([], originabot_ppa_id=10)
    _cambiar(contrato, [plantas["DOS"]])
    ob.falla = OriginabotError("originabot no respondió.")
    originabot_sync.sincronizar(contrato, "tok")

    contrato.refresh_from_db()
    assert contrato.originabot_sync_estado == "error"
    assert contrato.originabot_sync_detalle == "originabot no respondió."
    assert contrato.originabot_pendiente == {"agregar": [plantas["DOS"].id], "quitar": []}


# ── API ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def admin_local():
    from apps.plataforma.models import Usuario
    from apps.plataforma.services import cuentas

    u, _ = Usuario.objects.get_or_create(
        email="admin-ppa@unergy.io", defaults={"nombre": "A", "rol": "admin", "activo": True},
    )
    return {"HTTP_AUTHORIZATION": f"Bearer {cuentas.token_de(u, movil=False)}"}


def test_patch_con_login_propio_guarda_y_deja_pendiente(ob, plantas, admin_local):
    from django.test import Client

    contrato = _ppa([plantas["UNO"]], originabot_ppa_id=10)
    r = Client().patch(
        f"/api/v1/ppa/{contrato.id}", {"proyecto_ids": [plantas["UNO"].id, plantas["DOS"].id]},
        content_type="application/json", **admin_local,
    )
    assert r.status_code == 200
    # El cambio en Operaciones se guarda aunque no llegue a originabot.
    assert {p["id"] for p in r.json()["proyectos"]} == {plantas["UNO"].id, plantas["DOS"].id}
    assert r.json()["originabot_sync_estado"] == "pendiente"
    assert ob.parches == []


def test_opciones_sin_unergy_auth_es_403(admin_local):
    from django.test import Client

    r = Client().get("/api/v1/ppa/originabot/opciones", **admin_local)
    assert r.status_code == 403


def test_comando_vincular_minigranjas_dry_run_no_escribe(monkeypatch, plantas):
    """`vincular_minigranjas_originabot` llena la llave de las plantas con código;
    `--dry-run` solo informa."""
    from django.core.management import call_command

    from apps.proyectos.management.commands import vincular_minigranjas_originabot as cmd

    falso = OriginabotFalso()
    monkeypatch.setattr(cmd, "ClienteOriginabot", lambda token: falso)

    call_command("vincular_minigranjas_originabot", "--token", "tok", "--dry-run")
    plantas["UNO"].refresh_from_db()
    assert plantas["UNO"].originabot_minifarm_id is None

    call_command("vincular_minigranjas_originabot", "--token", "tok")
    for clave, esperado in (("UNO", 1), ("DOS", 2), ("TRES", 3), ("CINCO", None), ("SINTSF", None)):
        plantas[clave].refresh_from_db()
        assert plantas[clave].originabot_minifarm_id == esperado, clave
