"""El panel de ajuste de una reliquidación.

Cuando XM publica una versión corregida de un mes, el ER que se emite para esa
reliquidación ya trae la DIFERENCIA contra la versión anterior. El panel que se
arma con él no es un panel más del período: contabiliza solo **ingresos y
comercialización**.

Lo que estas pruebas fijan:

  * el OPEX no entra —los costos operativos se causaron una vez y no se
    reliquidan— y las facturas de servicio tampoco, porque un ajuste no emite
    factura (confirmado con Jessica el 2026-10-09);
  * el tipo del panel lleva la versión, para que un mes reliquidado dos veces
    tenga un panel por cada una y no se pisen (la unicidad de `panel_contable`
    es `(periodo, proyecto, tipo)`);
  * un tipo mal escrito se rechaza. `tipo` es la llave por la que filtra cada
    vista, así que un `ajsute_tx3` guardado en silencio crea un panel que nadie
    vuelve a ver.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    import django as dj

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    dj.setup()


@pytest.fixture
def mod():
    from apps.contabilidad.services import ajustes

    return ajustes


# ── Qué se contabiliza ──────────────────────────────────────────────────────

LINEAS = [
    {"grupo": "ingresos", "concepto": "Ingreso Bruto ENEL", "valor": 510_000.0},
    {"grupo": "comercializacion", "concepto": "Administración", "valor": -19_380.0},
    {"grupo": "comercializacion", "concepto": "Representación", "valor": -4_100.0},
    {"grupo": "costos", "concepto": "Mantenimiento", "valor": -800_000.0},
    {"grupo": "costos", "concepto": "IVA Mantenimiento", "valor": -152_000.0},
    {"grupo": "facturas", "concepto": "Factura de servicio", "valor": 90_000.0},
]


def test_el_ajuste_solo_lleva_ingresos_y_comercializacion(mod):
    grupos = {l["grupo"] for l in mod.solo_lineas_de_ajuste(LINEAS)}
    assert grupos == {"ingresos", "comercializacion"}


def test_el_opex_no_entra_al_ajuste(mod):
    # Los costos operativos se causaron en la liquidación original; volver a
    # contabilizarlos con el valor de la diferencia los duplicaría.
    conceptos = [l["concepto"] for l in mod.solo_lineas_de_ajuste(LINEAS)]
    assert "Mantenimiento" not in conceptos
    assert "IVA Mantenimiento" not in conceptos


def test_las_facturas_de_servicio_no_entran(mod):
    # Un ajuste no emite factura.
    assert all(l["grupo"] != "facturas" for l in mod.solo_lineas_de_ajuste(LINEAS))


def test_conserva_el_orden_original(mod):
    # La vista muestra ingresos antes que comercialización apoyándose en esto.
    assert [l["concepto"] for l in mod.solo_lineas_de_ajuste(LINEAS)] == [
        "Ingreso Bruto ENEL", "Administración", "Representación",
    ]


def test_una_linea_en_cero_se_conserva(mod):
    # Un concepto que la reliquidación dejó igual se revisó y no cambió; botarlo
    # lo volvería indistinguible de uno que la versión nueva no trajo.
    lineas = [{"grupo": "comercializacion", "concepto": "CGM", "valor": 0.0}]
    assert mod.solo_lineas_de_ajuste(lineas) == lineas


def test_un_ajuste_puede_ser_negativo(mod):
    # Si la versión nueva liquida menos, el ajuste es un contra-asiento.
    lineas = [{"grupo": "ingresos", "concepto": "Ingreso Bruto", "valor": -1_000.0}]
    assert mod.solo_lineas_de_ajuste(lineas)[0]["valor"] == -1_000.0


def test_sin_lineas_no_inventa_ninguna(mod):
    assert mod.solo_lineas_de_ajuste([]) == []


# ── El tipo del panel ───────────────────────────────────────────────────────

def test_el_tipo_lleva_la_version(mod):
    assert mod.tipo_de_ajuste("tx3") == "ajuste_tx3"


def test_dos_reliquidaciones_del_mismo_mes_no_se_pisan(mod):
    assert mod.tipo_de_ajuste("tx3") != mod.tipo_de_ajuste("tx4")


def test_el_tipo_cabe_en_la_columna(mod):
    # `panel_contable.tipo` es varchar(20); si algún día crece el prefijo o las
    # versiones, esto avisa antes que la base.
    assert all(len(t) <= 20 for t in mod.tipos_de_ajuste())


def test_reconoce_la_version_de_un_tipo_de_ajuste(mod):
    assert mod.version_de_ajuste("ajuste_tx3") == "tx3"


def test_el_oficial_no_es_un_ajuste(mod):
    assert not mod.es_ajuste("oficial")
    assert mod.version_de_ajuste("oficial") is None


def test_una_version_inventada_no_es_un_ajuste(mod):
    # `tx99` no existe en el catálogo de XM.
    assert not mod.es_ajuste("ajuste_tx99")


@pytest.mark.parametrize("tipo", ["preliquidacion", "oficial", "ajuste_tx3", "ajuste_tx8"])
def test_tipos_validos(mod, tipo):
    assert mod.tipo_valido(tipo)


@pytest.mark.parametrize("tipo", ["", None, "ajsute_tx3", "ajuste_", "ajuste_tx99", "oficiales"])
def test_tipos_rechazados(mod, tipo):
    assert not mod.tipo_valido(tipo)


@pytest.mark.parametrize("version", ["txf", "txr", "tx2"])
def test_las_versiones_anteriores_a_tx3_no_generan_ajuste(mod, version):
    # Una reliquidación es de tx3 en adelante. `txf` es la liquidación inicial
    # del mes y `txr`/`tx2` son pasos previos a ella: no hay nada contabilizado
    # antes contra lo cual ajustar, así que un `ajuste_txf` sería un panel que
    # duplica el mes entero.
    assert not mod.tipo_valido(mod.tipo_de_ajuste(version))


def test_el_error_nombra_los_tipos_posibles(mod):
    # Sin esto el mensaje diría solo «tipo inválido» y tocaría leer el código.
    mensaje = mod.error_de_tipo("ajsute_tx3")
    assert "ajsute_tx3" in mensaje
    assert "ajuste_tx3" in mensaje
    assert "oficial" in mensaje


def test_las_versiones_salen_del_catalogo_de_reliquidacion(mod):
    # Una segunda lista de versiones se desincronizaría de la de §4.9: las de
    # ajuste son la cola de ese catálogo, desde tx3.
    from apps.liquidaciones.services import reliquidacion

    assert mod.VERSIONES_AJUSTE == reliquidacion.VERSIONES[
        reliquidacion.VERSIONES.index("tx3"):
    ]
    assert mod.VERSIONES_AJUSTE[0] == "tx3"
