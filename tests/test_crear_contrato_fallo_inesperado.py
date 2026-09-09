"""Crear un contrato nunca puede terminar en un 500 mudo.

Bug real (2026-09-09): `POST /liquidaciones-api/contratos-energia` devolvía un
500 con la página HTML de Django. La usuaria lo vio dos veces y no tenía forma
de saber qué había pasado — y lo peor: **el contrato ya estaba creado**. El 130,
con sus tres proyectos vinculados. Reintentar habría creado un duplicado.

La API externa no ofrece transacción, así que el orden es crear → vincular →
cantidades, y cada paso necesita el id del anterior. El código ya contemplaba
que un paso fallara con `LiquidacionesAPIError` y respondía 502 diciendo qué
alcanzó a crearse. Lo que no contemplaba es que la respuesta viniera **sin
`id`** (`contrato["id"]`, `vinculo["id"]` → `KeyError`) ni que saltara cualquier
otro error inesperado: eso escapaba de DRF y se convertía en el 500 mudo.

Estas pruebas fijan la regla: pase lo que pase, la respuesta dice qué quedó
creado. Sin eso, el usuario duplica datos en producción sin saberlo.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


PAYLOAD = {
    "date_from": "2026-08-01",
    "date_to": "2026-08-04",
    "contract_type": "no_contract",
    "tariff_price_type": "market",
    "percentage": 1,
    "company": 61,
    "proyectos": [{"project": "agge_monterrey"}],
}


def _responder(monkeypatch, *, contrato, vinculo=None):
    """Sustituye las dos escrituras de la API externa por respuestas de mentira."""
    from api.v1.liquidaciones_proxy import views

    monkeypatch.setattr(views.api, "crear_contrato", lambda datos: contrato)

    def _vincular(datos):
        if isinstance(vinculo, Exception):
            raise vinculo
        return vinculo

    monkeypatch.setattr(views.api, "vincular_contrato_proyecto", _vincular)


def _llamar():
    from django.test import RequestFactory
    from rest_framework.test import force_authenticate

    from api.v1.liquidaciones_proxy import views
    from apps.plataforma.models import Usuario

    # Los permisos no son lo que se prueba aquí: se apagan para llegar a la vista.
    views.LiquidacionesApiViewSet.permission_classes = []
    views.LiquidacionesApiViewSet.get_permissions = lambda self: []
    vista = views.LiquidacionesApiViewSet.as_view({"post": "contratos_energia"})
    peticion = RequestFactory().post(
        "/api/v1/liquidaciones-api/contratos-energia",
        data=PAYLOAD, content_type="application/json",
    )
    force_authenticate(peticion, user=Usuario(id=1, email="qa@unergy.io", rol="admin"))
    respuesta = vista(peticion)
    respuesta.render()
    return respuesta


def test_contrato_sin_id_no_revienta(monkeypatch):
    """Si la API no devuelve el id del contrato, se avisa; no un KeyError."""
    _responder(monkeypatch, contrato={"date_from": "2026-08-01"})  # sin "id"
    r = _llamar()
    assert r.status_code != 500, r.content[:200]
    assert b"id" in r.content.lower()


def test_vinculo_sin_id_dice_que_el_contrato_ya_existe(monkeypatch):
    """El caso que producía el 500: el contrato quedó creado y hay que decirlo."""
    _responder(monkeypatch, contrato={"id": 130}, vinculo={"sin": "id"})
    r = _llamar()
    assert r.status_code != 500, r.content[:200]
    assert b"130" in r.content, "la respuesta debe nombrar el contrato ya creado"


def test_un_error_inesperado_tampoco_deja_el_500_mudo(monkeypatch):
    """Cualquier excepción, no solo las de la API, tiene que salir explicada."""
    _responder(monkeypatch, contrato={"id": 130}, vinculo=RuntimeError("algo raro"))
    r = _llamar()
    assert r.status_code != 500, r.content[:200]
    assert b"130" in r.content
