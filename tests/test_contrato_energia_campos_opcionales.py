"""Crear un contrato de energía «Sin contrato» no exige código.

Bug real (2026-09-09): `POST /liquidaciones-api/contratos-energia` respondía
400 y no se podía crear ningún contrato de este tipo.

`ContratoEnergiaSerializer` declaraba `code` y `company` obligatorios, pero la
API externa no los exige y el propio formulario tampoco: valida
«el código del contrato en XM es obligatorio salvo en "Sin contrato"» y omite
la clave cuando va vacía. El comercializador no lo validaba nadie.

Los datos de producción confirman cuál de las dos capas estaba mal — de los 106
contratos que existen:

  * los **20** de tipo `no_contract` tienen `codigo = None`, sin excepción
  * 25 de 106 no tienen código
  * 8 de 106 no tienen empresa

Así que el serializer era más estricto que la API a la que le habla. La vista ya
descarta los campos nulos antes de mandarlos (`if v is not None`), de modo que
omitirlos no cambia lo que viaja afuera.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _payload(**extra):
    """Lo que manda el formulario. Las claves vacías van como `undefined` en
    JavaScript, así que sencillamente no llegan."""
    base = {
        "date_from": "2026-09-01",
        "date_to": "2027-09-01",
        "contract_type": "no_contract",
        "tariff_price_type": "market",
        "proyectos": [
            {"project": "taurus_ix"},
            {"project": "taurus_viii"},
            {"project": "taurus_x"},
        ],
    }
    base.update(extra)
    return base


def _serializer(payload):
    from api.v1.liquidaciones_proxy.serializers import ContratoEnergiaSerializer

    return ContratoEnergiaSerializer(data=payload)


def test_sin_contrato_sin_codigo_es_valido():
    """El caso que devolvía 400: «Sin contrato» + Bolsa, sin código ni empresa."""
    s = _serializer(_payload())
    assert s.is_valid(), s.errors


def test_el_codigo_y_la_empresa_siguen_llegando_cuando_vienen():
    s = _serializer(_payload(code="ABC123", company=61))
    assert s.is_valid(), s.errors
    assert s.validated_data["code"] == "ABC123"
    assert s.validated_data["company"] == 61


def test_las_fechas_y_el_tipo_siguen_siendo_obligatorios():
    """Aflojar `code`/`company` no puede aflojar el resto."""
    for campo in ("date_from", "date_to", "contract_type"):
        payload = _payload()
        del payload[campo]
        s = _serializer(payload)
        assert not s.is_valid(), f"{campo} debería seguir siendo obligatorio"
        assert campo in s.errors
