"""La curva del CGM se guarda al clasificar; el panel NUNCA la pide en vivo.

`curva_cgm_referencia` es la cuarta curva de referencia de la fila, junto a las
de medidor principal/respaldo, Solenium y reconectador, y por la misma razon: se
consulta UNA vez en la corrida de madrugada (donde 2 segundos por frontera no le
importan a nadie) y el panel la lee de la base, que se abre muchas veces al dia.
Hay precedente explicito: Solenium dejo de consultarse en vivo en
`_construir_detalle` justamente porque costaba ~2s por apertura.

**Cambio del 2026-09-11: se elimino el ultimo resto de consulta en vivo.** Antes
quedaba una guarda, `_pedir_cgm_en_vivo()`, que permitia pedirsela a Quoia en un
caso: filas anteriores a la columna, con reporte automatico valido y otra fuente
elegida. La razon para quitarla es que el dato NO CAMBIA -- a diferencia de los
medidores y los inversores, que si se corrigen despues del cierre, el reporte
CGM de un dia ya cerrado es el que es. Si no cambia, no hay nada que refrescar.

Consecuencia asumida: en una fila anterior a la columna (existe desde el
2026-09-07), "Reportar con otra fuente" no ofrece el CGM. El dato sigue en Quoia
para quien lo necesite; lo que se dejo de pagar es una llamada de red en cada
apertura de cada frontera.

Lo que se prueba aca es lo que puede volver en silencio: que nadie reintroduzca
la llamada. Por eso el Quoia falso REVIENTA si se la piden -- una prueba que
mira el resultado no veria la diferencia, porque el detalle se sigue
construyendo igual.
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
def base_limpia():
    from django.db import transaction

    atomica = transaction.atomic()
    atomica.__enter__()
    yield
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


class _QuoiaQueNoAceptaPedidosDeCGM:
    """Responde el catalogo y las curvas, pero revienta si le piden el CGM.

    Es la unica forma de ver el cambio: el detalle se construye igual con o sin
    la llamada, asi que una prueba sobre el resultado no distinguiria nada.
    """

    def get_all_nodes(self):
        return [{"id": 10, "meter": {"id": 1}}]

    def get_all_borders(self):
        return [{"frt_generation": {"frt_code": "FRT001", "id": 7,
                                    "main_meter": 1, "backup_meter": None}}]

    def get_border_report_status(self, *a, **k):
        raise AssertionError(
            "el panel volvio a pedirle el reporte CGM a Quoia en vivo: ese dato "
            "no cambia despues del cierre y la llamada estaba en una ruta que se "
            "abre muchas veces al dia"
        )

    def get_border_report_status_con_estado(self, *a, **k):
        raise AssertionError("idem, por la variante con estado")

    def __getattr__(self, nombre):
        # Cualquier otra llamada (curvas de medicion) devuelve vacio: esta
        # prueba es sobre el CGM, no sobre las curvas.
        def _vacio(*a, **k):
            return []
        return _vacio


def _fila(**kw):
    """Una frontera de generacion con su reporte del dia."""
    from datetime import date

    from apps.energia.models import ReporteEnergiaGeneracion
    from apps.fronteras.models import Frontera
    from apps.proyectos.models import Proyecto

    proyecto = Proyecto.objects.create(nombre_comercial="MGS Prueba", potencia_ac_kw=990)
    frontera = Frontera.objects.create(
        proyecto=proyecto, codigo_frontera="FRT001",
        nombre_frontera="Frontera de prueba", tipo_frontera="generacion",
    )
    datos = dict(
        frontera_id=frontera.id,
        fecha=date(2026, 9, 7),
        caso=5,                      # obligatorio en el modelo; cual sea da igual acá
        curva_cgm_referencia=None,
        estado_reporte="OK",
        medidor_usado="historico",
    )
    datos.update(kw)
    ReporteEnergiaGeneracion.objects.create(**datos)
    return frontera, datos["fecha"]


def _detalle(frontera, fecha, monkeypatch):
    from apps.energia.services.reporte import vistas

    monkeypatch.setattr(vistas, "GaiaClient", _QuoiaQueNoAceptaPedidosDeCGM)
    return vistas._construir_detalle(frontera.id, fecha)


def test_la_fila_vieja_ya_no_dispara_la_llamada(base_limpia, monkeypatch):
    """El caso que la guarda permitia: sin curva guardada, reporte valido y
    otra fuente elegida. Antes preguntaba; ahora no."""
    frontera, fecha = _fila()

    detalle = _detalle(frontera, fecha, monkeypatch)

    assert detalle["curva_cgm"] is None


def test_una_fila_con_la_curva_guardada_la_devuelve(base_limpia, monkeypatch):
    """El caso normal desde que existe la columna: sale de la base."""
    curva = [1.5] * 24
    frontera, fecha = _fila(curva_cgm_referencia=curva)

    detalle = _detalle(frontera, fecha, monkeypatch)

    assert detalle["curva_cgm"] == curva


@pytest.mark.parametrize("estado", [None, "", "OK", "WARNING", "ERROR"])
def test_ningun_estado_del_reporte_dispara_la_llamada(base_limpia, monkeypatch, estado):
    """Antes el estado decidia si preguntar. Ahora no decide nada: no se
    pregunta nunca."""
    frontera, fecha = _fila(estado_reporte=estado)

    detalle = _detalle(frontera, fecha, monkeypatch)

    assert detalle["curva_cgm"] is None


def test_con_el_cgm_como_medidor_usado_tampoco(base_limpia, monkeypatch):
    frontera, fecha = _fila(medidor_usado="cgm", curva_cgm_referencia=[2.0] * 24)

    detalle = _detalle(frontera, fecha, monkeypatch)

    assert detalle["curva_cgm"] == [2.0] * 24


def test_la_guarda_vieja_ya_no_existe():
    """`_pedir_cgm_en_vivo` se elimino con la llamada. Si alguien la reintroduce
    sin leer esto, que sea a la vista."""
    from apps.energia.services.reporte import vistas

    assert not hasattr(vistas, "_pedir_cgm_en_vivo")


# ── La columna existe en las dos tablas ──────────────────────────────────────

@pytest.mark.parametrize("modelo", ["ReporteEnergiaGeneracion", "ReporteEnergiaConsumo"])
def test_las_dos_tablas_tienen_la_columna(modelo):
    """Generacion y Consumo -- el reporte del CGM aplica a las dos."""
    from apps.energia import models

    campos = {f.name for f in getattr(models, modelo)._meta.get_fields()}
    assert "curva_cgm_referencia" in campos
