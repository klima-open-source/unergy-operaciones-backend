"""Reliquidar un período: duplicar las facturas de XM a la versión nueva (§4.9).

Cuando XM publica una versión corregida hay que rehacer el ciclo con ella. El
paso que lo habilita es `xm_invoice_duplication_to_settlement`, que copia las
facturas de la versión vieja a la nueva. **Ese paso no existía en la
plataforma**: se podían correr FTP y Liquidar en `tx3` porque esos dos sí traían
el selector de versión, pero Repartir respondía 400 —«no hay facturas del
período»— porque las facturas seguían viviendo solo en `txf`. Así, reliquidar
era imposible desde la vista (reportado por Jessica el 2026-09-24).

Estas pruebas fijan las reglas de versiones de §4.9, que son las que deciden si
la llamada tiene sentido antes de gastar una ida a la API:

  * la versión nueva no puede ser la misma que la vieja;
  * la vieja tiene que ser ANTERIOR en el orden `txf < txr < tx2 < tx3 … < tx8`;
  * sin versión vieja, la nueva tiene que ser `txf` — que es el arranque normal
    del mes, no una reliquidación.

Se validan acá y no solo del lado de la API para que el error salga en español y
antes de tocar nada: una duplicación al revés crearía facturas en una versión
vieja y ensuciaría el período.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _validar():
    from apps.liquidaciones.services.reliquidacion import validar_versiones

    return validar_versiones


def test_una_reliquidacion_normal_pasa():
    _validar()("txf", "tx3")          # no levanta


def test_se_puede_encadenar_reliquidaciones():
    """XM puede publicar tx3 y después tx4 sobre el mismo mes."""
    _validar()("tx3", "tx4")


def test_la_misma_version_no_es_una_reliquidacion():
    with pytest.raises(ValueError, match="distinta"):
        _validar()("tx3", "tx3")


def test_no_se_puede_reliquidar_hacia_atras():
    """Duplicar tx4 -> tx3 crearía facturas en una versión ya cerrada."""
    with pytest.raises(ValueError, match="posterior"):
        _validar()("tx4", "tx3")


@pytest.mark.parametrize("vacia", [None, "", "   "])
def test_sin_version_vieja_la_nueva_tiene_que_ser_txf(vacia):
    """Sin versión previa no hay nada que reliquidar: es el arranque del mes."""
    _validar()(vacia, "txf")
    with pytest.raises(ValueError, match="txf"):
        _validar()(vacia, "tx3")


@pytest.mark.parametrize("mala", ["tx9", "TXF ", "txf3", "inventada"])
def test_una_version_que_no_existe_se_rechaza(mala):
    with pytest.raises(ValueError, match="no existe|válida"):
        _validar()("txf", mala)


def test_el_orden_es_el_de_la_guia():
    """`txf` es la primera y `tx8` la última; de ahí sale «anterior»."""
    from apps.liquidaciones.services.reliquidacion import VERSIONES

    assert VERSIONES[0] == "txf"
    assert VERSIONES[-1] == "tx8"
    assert VERSIONES.index("tx3") < VERSIONES.index("tx4")
