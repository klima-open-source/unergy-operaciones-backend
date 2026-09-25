"""El nombre del estado de resultados puede traer la versión al final.

Hoy la API **no** la escribe. Verificado el 2026-09-25 de tres formas sobre la
carpeta de Drive que lee la plataforma: ninguno de los 2.309 archivos la trae en
el nombre, los generados ese día pedidos por su propio id de Drive tampoco, y la
cadena `txf`/`tx3` no aparece dentro del xlsx. En el nombre del **cruce de
facturas** sí la escribe: `Cruce facturas 8 2026 txf.xlsx`.

El parser la acepta igual, opcional, para que el día que la agreguen la vista la
muestre sin que haya que tocar nada. Lo que estas pruebas cuidan es que aceptarla
**no rompa** los nombres de hoy: un mes y un año al final tienen que seguir
leyéndose igual, y la descripción no puede quedarse con la versión pegada.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _parse(nombre):
    from apps.contabilidad.services.drive import parse_nombre_er

    return parse_nombre_er(nombre)


# ── Los nombres de hoy, que son los que no se pueden romper ─────────────────

def test_el_nombre_sin_version_se_sigue_leyendo():
    r = _parse("Estado resultados Ayurá S.A.S. MGS 0009 El Molino 6 2025.xlsx")
    assert r["tipo"] == "estado_resultados"
    assert (r["mes"], r["anio"]) == (6, 2025)
    assert r["descripcion"] == "Ayurá S.A.S. MGS 0009 El Molino"
    assert r["version"] is None


def test_una_descripcion_con_numeros_no_confunde_al_periodo():
    """«MGS 0007» y «0004» son parte del nombre, no el mes."""
    r = _parse("Estado resultados Ayurá S.A.S. MGS 0007 La Paz Vallenata 6 2025.xlsx")
    assert (r["mes"], r["anio"]) == (6, 2025)
    assert "0007" in r["descripcion"]


def test_la_copia_sigue_marcandose():
    r = _parse("Copia de Estado resultados Solenium S.A.S El Son 6 2025.xlsx")
    assert r["es_copia"] is True
    assert (r["mes"], r["anio"]) == (6, 2025)


# ── La versión, para cuando la API la escriba ───────────────────────────────

def test_si_trae_version_se_lee():
    r = _parse("Estado resultados Solenium S.A.S El Son 6 2025 txf.xlsx")
    assert r["version"] == "txf"
    assert (r["mes"], r["anio"]) == (6, 2025)


def test_la_version_no_se_queda_pegada_a_la_descripcion():
    """Si se colara en `desc`, el nombre en pantalla saldría con la versión
    pegada y el filtro por versión no encontraría nada."""
    r = _parse("Estado resultados Solenium S.A.S El Son 6 2025 tx3.xlsx")
    assert r["descripcion"] == "Solenium S.A.S El Son"
    assert r["version"] == "tx3"


@pytest.mark.parametrize("v", ["txf", "txr", "tx2", "tx3", "tx8"])
def test_se_aceptan_todas_las_versiones_del_catalogo(v):
    assert _parse(f"Estado resultados Perijá 7 2026 {v}.xlsx")["version"] == v


def test_la_version_se_normaliza_a_minusculas():
    assert _parse("Estado resultados Perijá 7 2026 TXF.xlsx")["version"] == "txf"


def test_algo_que_no_es_una_version_no_se_toma_como_tal():
    """`tx9` no existe; el nombre completo queda como descripción."""
    r = _parse("Estado resultados Perijá 7 2026 tx9.xlsx")
    assert r["version"] is None


def test_el_cruce_sigue_trayendo_la_suya():
    r = _parse("Cruce facturas 8 2026 txf.xlsx")
    assert r["tipo"] == "cruce_facturas"
    assert r["version"] == "txf"
