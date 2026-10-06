"""El sondeo de MGS abre UNA sesión en Quoia por corrida, no dos.

Las alarmas de desconexión corren dentro del sondeo; antes cada una creaba su
`GaiaClient` y cada cliente inicia su propia sesión (96 logins al día de más).
Ningún test sale a la red.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def test_el_sondeo_le_pasa_su_cliente_a_las_alarmas_de_desconexion(monkeypatch):
    from apps.comun.integraciones import gaia_client
    from apps.monitoreo.services.alarmas import desconexion, sondeo

    creados = []

    class _Gaia:
        enabled = False  # corta el ciclo de MGS justo después de desconexión

        def __init__(self):
            creados.append(self)

    recibido = []
    monkeypatch.setattr(gaia_client, "GaiaClient", _Gaia)
    monkeypatch.setattr(desconexion, "evaluar_desconexiones", lambda g=None: recibido.append(g))

    sondeo.sondear()

    assert len(creados) == 1
    assert recibido == creados
