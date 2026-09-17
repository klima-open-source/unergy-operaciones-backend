"""De qué proyectos se puede sacar un diagrama fasorial.

El fasorial no dibuja generación: dibuja la lectura eléctrica de UN medidor.
Por eso su universo no es el de Generación Solar. `monitoreo_flota` lista
minigranjas en operación con servicio de operación —esa es la regla del
módulo, no un filtro que se pueda aflojar— y el selector del fasorial se
llenaba de ahí, así que solo se podía pedir el diagrama de una minigranja.

El caso que lo destapó (2026-09-17) es el autoconsumo de **Nestlé DPA**
(proyecto 59, medidor serie 88865813 → nodo Gaia 1670). Un autoconsumo no
entrega energía al SIC: no tiene frontera de generación y Gaia no lo publica
como border, así que el mapa dinámico —que se arma desde los borders— no lo
encuentra por más que el nodo exista y reporte. Es el mismo caso de San Pedro,
y se resuelve igual: `_PROYECTO_NODE_OVERRIDE`.

Lo que se fija acá son las dos mitades que hacen falta para que el botón sirva:

  · que el proyecto RESUELVA a su nodo (si no, el detalle viene sin snapshot), y
  · que el proyecto se pueda ELEGIR (si no, el diagrama existe pero nadie llega
    a pedirlo).
"""
import pytest


# ── Resolver el nodo ────────────────────────────────────────────────────────


def test_nestle_resuelve_su_nodo_sin_border_en_gaia():
    """Sin frontera en la BD y sin preguntarle a Gaia: el override alcanza.

    `db_proyecto_frt_map` vacío es exactamente la situación real de un
    autoconsumo, y `gaia=None` es el peor caso (API caída). Aun así tiene que
    salir el nodo, porque el override va ANTES de todo lo demás.
    """
    from app.services.mgs.gaia_client import find_gaia_node_pair

    assert find_gaia_node_pair(proyecto_id=59, db_proyecto_frt_map={}) == (1670, None)


def test_sin_respaldo_es_none_no_el_principal_repetido():
    """Nestlé tiene un solo medidor. El respaldo no se inventa: el modal ofrece
    Principal/Respaldo y con un respaldo falso dibujaría dos veces lo mismo."""
    from app.services.mgs.gaia_client import find_gaia_node_pair

    _, respaldo = find_gaia_node_pair(proyecto_id=59, db_proyecto_frt_map={})
    assert respaldo is None


def test_un_proyecto_sin_vinculo_sigue_sin_nodo():
    """El override es una lista explícita, no una adivinanza: lo que no está
    en ella se sigue reportando sin nodo."""
    from app.services.mgs.gaia_client import find_gaia_node_pair

    assert find_gaia_node_pair(proyecto_id=99999, db_proyecto_frt_map={}) == (None, None)


def test_el_override_no_le_gana_a_las_fronteras_de_los_demas():
    """Agregar un override no puede cambiarle el nodo a quien ya lo resolvía
    por su frontera."""
    from app.services.mgs.gaia_client import find_gaia_node_pair

    assert find_gaia_node_pair(
        proyecto_id=7, db_proyecto_frt_map={7: "frt55044"},
    ) == (603, 604)


# ── Poder elegirlo ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def _base():
    """Django solo para esta mitad del archivo.

    El skip va en el fixture y no en el módulo a propósito: la resolución del
    nodo se prueba contra `gaia_client` pelado, que no sabe de framework, y
    esos tests tienen que correr igual sin el entorno completo.
    """
    import os

    django = pytest.importorskip(
        "django", reason="requiere el entorno de Django (uv sync)")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()
    return django


class _ConsultaFalsa:
    """Lo mínimo de un queryset que usa `proyectos_con_medidor`: filtrar y
    pedir columnas. Guarda los filtros para poder revisarlos."""

    def __init__(self, filas, registro=None):
        self._filas = filas
        self.filtros = registro if registro is not None else {}

    def filter(self, **kw):
        self.filtros.update(kw)
        return self

    def values_list(self, *campos):
        return [tuple(f[c] for c in campos) for f in self._filas]


@pytest.fixture
def sv(_base, monkeypatch):
    """El módulo con el caché vacío, sin BD y sin Gaia."""
    from apps.energia.services import solarview_monitoreo as modulo

    modulo._cache.clear()
    # Sin credenciales de Gaia la resolución cae al mapa estático, que es lo
    # que se quiere acá: el override no depende de la API externa.
    monkeypatch.setattr(modulo, "_get_gaia", lambda: None)
    yield modulo
    modulo._cache.clear()


def _montar(sv, monkeypatch, *, fronteras, proyectos):
    filtros_proy: dict = {}
    monkeypatch.setattr(
        sv, "Frontera", type("F", (), {"objects": _ConsultaFalsa(fronteras)}),
    )
    monkeypatch.setattr(
        sv, "Proyecto",
        type("P", (), {"objects": _ConsultaFalsa(proyectos, filtros_proy)}),
    )
    return filtros_proy


def test_nestle_entra_a_la_lista_aunque_no_tenga_frontera(sv, monkeypatch):
    """La mitad que faltaba: el proyecto del override se puede ELEGIR.

    Ninguna frontera lo nombra —es un autoconsumo— y aun así tiene que estar
    entre los candidatos que se consultan y salir en la respuesta.
    """
    filtros = _montar(
        sv, monkeypatch,
        fronteras=[{"proyecto_id": 7, "codigo_frontera": "FRT55044"}],
        proyectos=[
            {"id": 7, "nombre_comercial": "Minigranja Solar Baraya"},
            {"id": 59, "nombre_comercial": "Nestlé DPA"},
        ],
    )

    salida = sv.proyectos_con_medidor()

    assert 59 in filtros["id__in"], "el override no llegó a los candidatos"
    assert {"proyecto_id": 59, "nombre": "Nestlé DPA"} in salida["projects"]


def test_no_lista_proyectos_que_no_resuelven_a_ningun_nodo(sv, monkeypatch):
    """Tener fila de frontera no es tener medidor: si el código no resuelve a
    un nodo, el proyecto no se ofrece. Elegirlo solo daría 'sin datos'."""
    _montar(
        sv, monkeypatch,
        fronteras=[{"proyecto_id": 8, "codigo_frontera": "frt_inexistente"}],
        proyectos=[{"id": 8, "nombre_comercial": "Proyecto sin nodo"}],
    )

    assert sv.proyectos_con_medidor()["projects"] == []


def test_los_borrados_no_se_ofrecen(sv, monkeypatch):
    filtros = _montar(
        sv, monkeypatch,
        fronteras=[],
        proyectos=[{"id": 59, "nombre_comercial": "Nestlé DPA"}],
    )

    sv.proyectos_con_medidor()

    assert filtros.get("deleted_at__isnull") is True


def test_la_lista_va_alfabetica(sv, monkeypatch):
    """Es un desplegable con filtro: el orden es para el que busca a ojo."""
    _montar(
        sv, monkeypatch,
        fronteras=[{"proyecto_id": 7, "codigo_frontera": "frt55044"}],
        proyectos=[
            {"id": 59, "nombre_comercial": "Nestlé DPA"},
            {"id": 7, "nombre_comercial": "Minigranja Solar Baraya"},
        ],
    )

    nombres = [p["nombre"] for p in sv.proyectos_con_medidor()["projects"]]
    assert nombres == ["Minigranja Solar Baraya", "Nestlé DPA"]
