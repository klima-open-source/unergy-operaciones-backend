"""Cambiar la planta de una solicitud GESCON tiene que *cambiarla*.

El 2026-09-15 Jessica reportó que el contrato `CTLP0003368` estaba en GD
Marimonda cuando debía estar en San Pelayo, y que la vista «no la dejaba
cambiar». No era la vista: el PATCH respondía 200, salía el aviso de
«Actualizado», y la planta seguía igual.

La causa es una asimetría entre los dos serializers. El de LECTURA emite
``proyecto_id`` y ``contrato_ppa_id`` —y eso es lo que el formulario devuelve—,
pero el de ESCRITURA es un ``ModelSerializer`` y sus campos se llaman como las
claves foráneas del modelo: ``proyecto`` y ``contrato_ppa``. DRF **descarta en
silencio** las claves que no reconoce, así que ``{"proyecto_id": 42}`` validaba
sin errores y dejaba ``validated_data`` VACÍO.

Un campo que no existe da 400 y se nota. Este daba 200 y no hacía nada: el peor
de los dos modos de fallar, porque la usuaria cree que guardó.

Estas pruebas fijan que el lado de escritura acepte los MISMOS nombres que emite
el de lectura. Se quedan en la declaración de los campos —sin base de datos—
porque el repo no tiene `pytest-django`; eso basta para que la asimetría no
vuelva a colarse.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _campos(clase_nombre):
    from api.v1.asic import serializers as asic

    return getattr(asic, clase_nombre)().fields


@pytest.mark.parametrize("campo, destino", [
    ("proyecto_id", "proyecto"),
    ("contrato_ppa_id", "contrato_ppa"),
])
def test_escritura_acepta_el_nombre_que_emite_lectura(campo, destino):
    """Sin esto, el PATCH del formulario responde 200 y no cambia nada."""
    campos = _campos("SolicitudEscrituraSerializer")
    assert campo in campos, (
        f"El formulario manda «{campo}» y el serializer de escritura no lo "
        f"declara: DRF lo descarta en silencio y el PATCH no guarda nada."
    )
    assert campos[campo].source == destino


@pytest.mark.parametrize("campo", ["proyecto_id", "contrato_ppa_id"])
def test_lectura_y_escritura_hablan_el_mismo_idioma(campo):
    """La asimetría entre los dos lados es la que causó el fallo."""
    assert campo in _campos("SolicitudSerializer")
    assert campo in _campos("SolicitudEscrituraSerializer")


@pytest.mark.parametrize("campo", ["proyecto_id", "contrato_ppa_id"])
def test_la_planta_se_puede_dejar_vacia(campo):
    """Una terminación manda `proyecto_id: null` a propósito: sin planta,
    Cumplimiento prorratea el mes en vez de borrarlo."""
    campos = _campos("SolicitudEscrituraSerializer")
    assert campos[campo].allow_null is True
    assert campos[campo].required is False


@pytest.mark.parametrize("viejo", ["proyecto", "contrato_ppa"])
def test_el_nombre_viejo_sigue_sirviendo(viejo):
    """No se rompe a quien ya mandaba el nombre de la clave foránea."""
    assert viejo in _campos("SolicitudEscrituraSerializer")
