"""GaiaClient.ultima_llamada_fallo -- distinguir "Quoia no tiene el dato" de
"no se pudo preguntar" (diagnóstico de Fronteras, 2026-08-24).

Antes, `_get()` tragaba cualquier excepción y devolvía None -- indistinguible
de una respuesta vacía real. Esto hacía que /fronteras/quoia/pendientes
reportara "0 pendientes" durante una caída de Quoia (en vez de un error), y
que los backfills marcaran fronteras reales como "código ya no existe en
Quoia" / "sin info en Quoia"."""
import apps.comun.integraciones.gaia_client as gaia_client
from apps.comun.integraciones.gaia_client import _get_dynamic_maps


class _GaiaParcial:
    """Simula get_all_borders() fallando y get_all_nodes() con éxito -- el
    caso que podía enmascarar la falla real si solo se mira el resultado de
    la ÚLTIMA llamada."""
    def __init__(self):
        self.ultima_llamada_fallo = False

    def get_all_borders(self):
        self.ultima_llamada_fallo = True
        return []

    def get_all_nodes(self):
        self.ultima_llamada_fallo = False  # esta sí "tuvo éxito"
        return []


def test_fallo_en_borders_no_se_enmascara_por_exito_en_nodes(monkeypatch):
    monkeypatch.setattr(gaia_client, "_dynamic_cache", None)
    monkeypatch.setattr(gaia_client, "_dynamic_cache_ts", 0.0)

    gaia = _GaiaParcial()
    resultado = _get_dynamic_maps(gaia)

    assert resultado is None  # sin cache previo, no hay nada que devolver
    assert gaia.ultima_llamada_fallo is True


def test_exito_en_ambas_deja_la_bandera_en_false(monkeypatch):
    monkeypatch.setattr(gaia_client, "_dynamic_cache", None)
    monkeypatch.setattr(gaia_client, "_dynamic_cache_ts", 0.0)

    class _GaiaOk:
        def __init__(self):
            self.ultima_llamada_fallo = False

        def get_all_borders(self):
            return []

        def get_all_nodes(self):
            return []

    gaia = _GaiaOk()
    _get_dynamic_maps(gaia)

    assert gaia.ultima_llamada_fallo is False
