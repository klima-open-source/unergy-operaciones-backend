"""El emparejamiento de las sugerencias: contra duplicados y contra ambigüedad.

Dos cambios que van juntos porque atacan el mismo problema desde dos vistas
distintas: reconocer que un candidato externo YA existe en la base.

**`sub_project` como ancla** (`/proyectos/pendientes`). La cascada de
emparejamiento probaba `sunfactory_project_id`, `origina_code`, `codigo_tsf` y
`project_id_solenium`, y si ninguno daba, caía al nombre normalizado. Faltaba
`sub_project`, que es como identifica cada planta la API de Unergy (su
`nombre_topico`) -- así que al sumar esa fuente sus candidatos habrían caído
siempre al último recurso, que es exactamente como se crean los duplicados.

De paso se retira `project_id_solenium` de la cascada: **ninguna fuente lo
asigna nunca**. Es un resto de cuando Solenium era fuente de candidatos; al
migrar a SolarView se quitó la fuente y quedó el emparejamiento, comparando
siempre `None` contra el índice.

**La sugerencia por nombre** (`/fronteras/quoia/pendientes`). Solo funcionaba
con nombres tipo "MGS | Minigranja | MGR + número". Caso real del 2026-09-14:
Quoia trae "Calipso", "Rigel" y "Titán", y en la base están como "Astrea 1
(Calipso)", "Astrea 2 (Rigel)" y "Astrea 3 (Titan)" -- el proyecto existe, el
nombre de Quoia está ahí entre paréntesis, y no se sugería nada.

Lo delicado de ese arreglo es lo que NO debe hacer: sugerir cuando hay dudas.
Una sugerencia ambigua es peor que ninguna, porque quien confirma no vuelve a
mirar y la frontera queda colgada del proyecto equivocado.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


# ── La sugerencia por nombre ────────────────────────────────────────────────


def _sugerir(nombre_quoia, proyectos):
    from api.v1.fronteras.views import _sugerir_por_nombre

    return _sugerir_por_nombre(nombre_quoia, proyectos)


ASTREA = [
    (274, "Astrea 1 (Calipso)"),
    (273, "Astrea 2 (Rigel)"),
    (272, "Astrea 3 (Titan)"),
    (100, "MGS 0021 Ibirico"),
]


@pytest.mark.parametrize("quoia,esperado", [
    ("Calipso", 274),
    ("Rigel", 273),
    ("Titan", 272),
])
def test_encuentra_el_proyecto_por_el_nombre_entre_parentesis(quoia, esperado):
    """El caso que motivó el cambio."""
    assert _sugerir(quoia, ASTREA)[0] == esperado


def test_no_distingue_mayusculas():
    assert _sugerir("CALIPSO", ASTREA)[0] == 274
    assert _sugerir("calipso", ASTREA)[0] == 274


def test_ignora_espacios_alrededor():
    assert _sugerir("  Calipso  ", ASTREA)[0] == 274


def test_sin_coincidencia_no_sugiere():
    """Caracolí no es planta nuestra: no debe salir emparejada con nada."""
    assert _sugerir("Caracoli", ASTREA) is None


def test_con_dos_coincidencias_no_sugiere_nada():
    """Lo más importante de la prueba: ante la duda, ninguna. Quien confirma no
    vuelve a mirar, y una frontera colgada del proyecto equivocado es peor que
    una sin sugerir."""
    proyectos = [(1, "Astrea 1 (Calipso)"), (2, "Astrea 1 (Calipso) Fase 2")]

    assert _sugerir("Calipso", proyectos) is None


@pytest.mark.parametrize("corto", ["GD", "N2", "Sur", "A", ""])
def test_un_nombre_muy_corto_no_sugiere(corto):
    """"GD" emparejaría con media base."""
    proyectos = [(1, "GD Biosolar"), (2, "GD Delta 1"), (3, "Planta Sur")]

    assert _sugerir(corto, proyectos) is None


def test_un_nombre_largo_si_sugiere_aunque_sea_unico():
    proyectos = [(1, "GD Biosolar"), (2, "GD Delta 1")]

    assert _sugerir("Biosolar", proyectos)[0] == 1


def test_el_nombre_en_none_no_revienta():
    assert _sugerir(None, ASTREA) is None


def test_un_proyecto_sin_nombre_no_revienta():
    assert _sugerir("Calipso", [(1, None), (274, "Astrea 1 (Calipso)")])[0] == 274


def test_el_camino_viejo_sigue_primero():
    """`_mgs_number` no se toca: la sugerencia por nombre es el SEGUNDO intento,
    no un reemplazo."""
    import inspect

    from api.v1.fronteras.views import FronteraViewSet

    fuente = inspect.getsource(FronteraViewSet.quoia_pendientes)

    assert "por_numero.get(_mgs_number(nombre_quoia))" in fuente
    assert "or _sugerir_por_nombre(" in fuente


# ── El ancla de `sub_project` ───────────────────────────────────────────────


def test_sub_project_entra_en_la_cascada():
    import inspect

    from apps.proyectos.services import pendientes

    fuente = inspect.getsource(pendientes.resolver_pendientes)

    assert "por_sub_project = {" in fuente
    assert "match = por_sub_project.get(c.sub_project.lower())" in fuente


def test_el_ancla_va_antes_del_nombre():
    """El orden es el punto: un identificador exacto tiene que probarse ANTES
    que la coincidencia por nombre, que es el último recurso."""
    import inspect

    from apps.proyectos.services import pendientes

    fuente = inspect.getsource(pendientes.resolver_pendientes)

    assert fuente.index("por_sub_project.get") < fuente.index("por_core.get(c.core)")


def test_solenium_sale_de_la_cascada():
    """Ninguna fuente asignaba `project_id_solenium`: comparaba `None` contra el
    índice, siempre."""
    import inspect

    from apps.proyectos.services import pendientes

    fuente = inspect.getsource(pendientes.resolver_pendientes)

    assert "por_solenium_id" not in fuente


def test_la_clave_sigue_en_la_respuesta():
    """Se conserva, en None, para no romper al frontend que la lee."""
    import inspect

    from apps.proyectos.services import pendientes

    fuente = inspect.getsource(pendientes.resolver_pendientes)

    assert '"project_id_solenium": None,' in fuente
