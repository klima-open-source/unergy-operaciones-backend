"""La API de Unergy como tercera fuente de proyectos por sugerir.

Hacía falta porque hay plantas que **solo** existen ahí: las de terceros a las
que les prestamos algún servicio. El caso que lo motivó (2026-09-14):
"Caracolí" aparecía como frontera pendiente en Quoia y nunca como proyecto por
sugerir, porque ni Sun Factory ni Quoia la conocían como proyecto.

`nombre_topico` es su identificador, y es el mismo valor que guarda
`Proyecto.sub_project`. Por eso `sub_project` tuvo que entrar ANTES en la
cascada de emparejamiento: sin ese ancla, cada planta de acá habría caído a la
coincidencia por nombre, que es como se crean los duplicados.

Y el caché no es un extra: `/proyectos/pendientes` consulta esta API en CADA
llamada, y esa API no es barata. Lo que se fija acá es que no se repita el
trabajo, y que un fallo **no** se guarde -- una lista vacía por API caída es
indistinguible de "no hay proyectos", y cachearla congelaría el error media
hora.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


# ── La fuente ───────────────────────────────────────────────────────────────


@pytest.fixture
def unergy(monkeypatch):
    from apps.energia.services import comercializacion

    estado = {"proyectos": [], "token": "tk", "llamadas": 0}

    def _fetch(token):
        estado["llamadas"] += 1
        return list(estado["proyectos"])

    monkeypatch.setattr(comercializacion, "unergy_token", lambda: estado["token"])
    monkeypatch.setattr(
        comercializacion, "fetch_unergy_projects_cacheado", lambda t: _fetch(t)
    )
    return estado


def _candidatos():
    from apps.proyectos.services.pendientes import _candidatos_unergy

    return _candidatos_unergy()


def test_cada_planta_llega_con_su_identificador(unergy):
    """`nombre_topico` -> `sub_project`: es el ancla contra duplicados."""
    unergy["proyectos"] = [
        {"nombre_topico": "caracoli", "nombre_proyecto": "Caracolí"},
    ]

    [c] = _candidatos()

    assert c.sub_project == "caracoli"
    assert c.nombre_raw == "Caracolí"
    assert c.fuentes == {"unergy"}


def test_sin_topico_no_es_candidato(unergy):
    """Sin identificador no hay ancla, y sin ancla el emparejamiento cae al
    nombre -- que es justo lo que se quiere evitar."""
    unergy["proyectos"] = [{"nombre_proyecto": "Sin Tópico"}]

    assert _candidatos() == []


def test_sin_nombre_no_es_candidato(unergy):
    unergy["proyectos"] = [{"nombre_topico": "x1"}]

    assert _candidatos() == []


def test_cae_a_nombre_corto_si_no_hay_nombre_de_proyecto(unergy):
    unergy["proyectos"] = [{"nombre_topico": "cal", "nombre_corto": "Calipso"}]

    assert _candidatos()[0].nombre_raw == "Calipso"


def test_un_fallo_de_token_no_tumba_los_pendientes(unergy, monkeypatch):
    """Las otras dos fuentes tienen que seguir sirviendo."""
    from apps.energia.services import comercializacion

    def _reventar():
        raise RuntimeError("sin credenciales")

    monkeypatch.setattr(comercializacion, "unergy_token", _reventar)

    assert _candidatos() == []


def test_un_fallo_de_la_api_tampoco(unergy, monkeypatch):
    from apps.energia.services import comercializacion

    def _reventar(token):
        raise RuntimeError("API caída")

    monkeypatch.setattr(comercializacion, "fetch_unergy_projects_cacheado", _reventar)

    assert _candidatos() == []


def test_entra_en_la_lista_de_fuentes():
    import inspect

    from apps.proyectos.services import pendientes

    fuente = inspect.getsource(pendientes.resolver_pendientes)

    assert "_candidatos_unergy()" in fuente


# ── El caché ────────────────────────────────────────────────────────────────


@pytest.fixture
def cache_limpio(monkeypatch):
    from django.test import override_settings

    from apps.energia.services import comercializacion

    comercializacion._cache_proyectos.clear()
    estado = {"llamadas": 0, "respuesta": [{"nombre_topico": "a", "nombre_proyecto": "A"}]}

    def _real(token):
        estado["llamadas"] += 1
        return list(estado["respuesta"])

    monkeypatch.setattr(comercializacion, "fetch_unergy_projects", _real)

    with override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    ):
        from django.core.cache import cache

        cache.clear()
        yield estado
    comercializacion._cache_proyectos.clear()


def _pedir():
    from apps.energia.services.comercializacion import fetch_unergy_projects_cacheado

    return fetch_unergy_projects_cacheado("tk")


def test_la_segunda_vez_sale_del_cache(cache_limpio):
    _pedir()
    _pedir()

    assert cache_limpio["llamadas"] == 1


def test_lo_guardado_lo_ve_otro_worker(cache_limpio):
    from apps.energia.services import comercializacion

    _pedir()
    comercializacion._cache_proyectos.clear()  # otro proceso de gunicorn

    _pedir()

    assert cache_limpio["llamadas"] == 1


def test_una_lista_vacia_no_se_cachea(cache_limpio):
    """Una API caída devuelve `[]`, igual que "no hay proyectos". Guardarlo
    congelaría el error media hora."""
    cache_limpio["respuesta"] = []

    _pedir()
    _pedir()

    assert cache_limpio["llamadas"] == 2


def test_tras_el_fallo_el_exito_si_se_guarda(cache_limpio):
    cache_limpio["respuesta"] = []
    _pedir()                                    # llamada 1: vacia, no se guarda

    cache_limpio["respuesta"] = [{"nombre_topico": "a", "nombre_proyecto": "A"}]
    _pedir()                                    # llamada 2: sale bien, se guarda
    _pedir()                                    # del cache

    assert cache_limpio["llamadas"] == 2


def test_sin_redis_sigue_funcionando(cache_limpio, monkeypatch):
    from django.core.cache import cache

    def _reventar(*a, **kw):
        raise RuntimeError("Redis no responde")

    monkeypatch.setattr(cache, "get", _reventar)
    monkeypatch.setattr(cache, "set", _reventar)

    _pedir()
    _pedir()

    assert cache_limpio["llamadas"] == 1, "el cache por proceso debía seguir"


def test_los_backfills_no_pasan_por_el_cache():
    """Corren a mano y quieren el dato fresco."""
    import inspect

    from apps.proyectos.services import backfill_unergy

    fuente = inspect.getsource(backfill_unergy)

    assert "fetch_unergy_projects_cacheado" not in fuente
