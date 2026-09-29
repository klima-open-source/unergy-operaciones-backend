"""El estado de los reconectadores se lee de SolarView; el ON/OFF sigue en Solenium.

`GET api.sole.tech/solarview/config/recloser/?project_id=` devuelve la medición
más reciente con las mismas claves que traía `data.sole.tech` (`active`,
`time`, `i_a`… `pf`). Verificado en vivo el 2026-09-29. El comando ON/OFF no
tiene endpoint documentado en SolarView todavía, así que no se mueve.
"""
from types import SimpleNamespace

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


class _SolarView:
    """Contesta como SolarView: medidas para los ids que tienen relay, None
    (el 404 del cliente) para los demás."""

    _base_url = "https://api.sole.tech"
    enabled = True

    def __init__(self, con_relay: dict[int, dict]):
        self.con_relay = con_relay
        self.pedidos: list[tuple[str, dict]] = []

    def _get(self, url, params=None):
        self.pedidos.append((url, params))
        medidas = self.con_relay.get((params or {}).get("project_id"))
        return {"results": medidas} if medidas is not None else None


@pytest.fixture
def relay(monkeypatch):
    from apps.monitoreo.services import reconectadores

    falso = _SolarView({17: {"active": True, "time": "2026-09-29 17:32:18", "kw": 5.0, "i_a": "1.5"}})
    monkeypatch.setattr(reconectadores, "cliente", lambda: falso)
    return reconectadores, falso


def test_la_lectura_va_a_solarview_con_project_id(relay):
    reconectadores, falso = relay

    tiene, medidas = reconectadores.leer_relay(17)

    assert tiene
    assert medidas["active"] is True
    assert falso.pedidos == [("https://api.sole.tech/solarview/config/recloser/", {"project_id": 17})]


def test_una_planta_sin_reconectador_se_omite(relay):
    reconectadores, _ = relay

    assert reconectadores.leer_relay(135) == (False, {})


def test_los_estados_usan_project_id_solarview(relay):
    reconectadores, falso = relay
    proyectos = [
        SimpleNamespace(id=1, nombre_comercial="La Paz Verso", project_id_solarview="17",
                        project_id_solenium="999"),
        SimpleNamespace(id=2, nombre_comercial="Sin relay", project_id_solarview="135",
                        project_id_solenium=None),
        SimpleNamespace(id=3, nombre_comercial="Sin id", project_id_solarview=None,
                        project_id_solenium="5"),
    ]

    estados = reconectadores.estados_de(proyectos)

    assert [e["proyecto_id"] for e in estados] == [1]
    assert estados[0]["sol_id"] == 17
    assert estados[0]["active"] is True
    assert estados[0]["potencia_kw"] == 5.0
    assert estados[0]["corriente_a"] == 1.5, "el texto se convierte a número"
    assert sorted(p[1]["project_id"] for p in falso.pedidos) == [17, 135]


def test_el_listado_filtra_por_project_id_solarview():
    from api.v1.reconectadores.queryset import proyectos_con_relay

    # Solo el WHERE: el SELECT lista todas las columnas de `proyectos`.
    filtros = str(proyectos_con_relay().query).split(" WHERE ", 1)[1]

    assert "project_id_solarview" in filtros
    assert "project_id_solenium" not in filtros


def test_el_on_off_sigue_en_solenium_con_las_credenciales_del_usuario():
    """No se mueve hasta que SolarView documente cómo mandar el comando."""
    from apps.monitoreo.services import reconectadores

    assert reconectadores.RELAY_SET.endswith("/project/{sol_id}/relay/set-status/")
    assert "data.sole.tech" in reconectadores.RELAY_SET
    assert reconectadores.AUTH_URL.endswith("/token/")
