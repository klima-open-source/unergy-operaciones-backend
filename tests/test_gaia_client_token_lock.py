"""GaiaClient._token_lock -- _authenticate()/_try_refresh() no deben poder
correr al mismo tiempo en dos hilos del mismo cliente.

Sin el lock, dos hilos usando el mismo GaiaClient en paralelo (ver
curva_medidor_en_vivo -- principal+respaldo a la vez) podían renovar el
token cada uno por su cuenta y pisarse el resultado si uno de los dos
fallaba."""
import threading
import time
import types

from apps.comun.integraciones.gaia_client import GaiaClient


class _RespuestaOk:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"access": "token-nuevo", "refresh": "refresh-nuevo"}


def _post_lento(contador, lock_contador, demora=0.05):
    def _post(*a, **kw):
        with lock_contador:
            contador["activos"] += 1
            contador["max"] = max(contador["max"], contador["activos"])
        time.sleep(demora)  # ventana amplia para que otro hilo intente colarse
        with lock_contador:
            contador["activos"] -= 1
        return _RespuestaOk()
    return _post


def test_authenticate_serializa_entre_hilos():
    client = GaiaClient()
    contador = {"activos": 0, "max": 0}
    lock_contador = threading.Lock()
    client._http = types.SimpleNamespace(post=_post_lento(contador, lock_contador))

    hilos = [threading.Thread(target=client._authenticate) for _ in range(5)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert contador["max"] == 1
    assert client._access_token == "token-nuevo"


def test_try_refresh_serializa_entre_hilos():
    client = GaiaClient()
    client._refresh_token = "algun-refresh-token"
    contador = {"activos": 0, "max": 0}
    lock_contador = threading.Lock()
    client._http = types.SimpleNamespace(post=_post_lento(contador, lock_contador))

    hilos = [threading.Thread(target=client._try_refresh) for _ in range(5)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert contador["max"] == 1
