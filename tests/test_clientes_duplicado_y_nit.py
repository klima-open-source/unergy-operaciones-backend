"""POST /clientes: aviso de nombre parecido, choque de NIT y lista de contactos.

Tres cosas que ya estaban cubiertas (el 409 estructurado de nombre parecido, que
el frontend no conectaba -- auditoria de Clientes 2026-08-27 --, `forzar` y el
NIT duplicado que reventaba con 500) mas las dos que aparecieron el 2026-09-10
revisando por que una compañera no podia crear un cliente:

1. **Dos contactos con el mismo correo y tipo tumbaban el alta con un 500.**
   `contactos` tiene UNIQUE (cliente_id, email, tipo) y el formulario permite
   agregar dos renglones iguales. Repetir a la misma persona en el formulario no
   es un error que valga tumbar nada: se guarda una vez.
2. **Y el cliente quedaba creado igual.** `Cliente.objects.create()` hacia su
   propio commit y los contactos iban despues, sin transaccion que los una: si
   ese INSERT fallaba, el 500 dejaba el cliente en la base. El usuario veia un
   error, reintentaba, y entonces chocaba con el NIT de la fila que su intento
   anterior SI habia dejado -- se veia como "no puedo crear este cliente" dos
   veces por dos motivos distintos. En FastAPI no pasaba porque era un `flush`
   mas un unico `commit` (app/api/v1/clientes.py): la atomicidad se perdio en el
   port a Django.

Estas pruebas llamaban a `app/api/v1/clientes.py`, el arbol FastAPI apagado.
Ahora apuntan al ViewSet de Django, que es lo que se sirve.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)

    from django.conf import settings

    originales = settings.DATABASES
    settings.DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    }
    django.setup()

    from django.apps import apps as django_apps
    from django.db import connections
    from django.test.utils import setup_test_environment

    connections.close_all()
    connections.__dict__.pop("settings", None)
    connections.__init__()

    settings.MIGRATION_MODULES = {a.label: None for a in django_apps.get_app_configs()}
    setup_test_environment()
    connections["default"].creation.create_test_db(verbosity=0)
    assert connections["default"].vendor == "sqlite", "no se aisló de la base real"
    yield

    from django.test.utils import teardown_test_environment

    connections.close_all()
    teardown_test_environment()
    settings.DATABASES = originales
    connections.__dict__.pop("settings", None)
    connections.__init__()


@pytest.fixture
def datos():
    from django.db import transaction

    from apps.plataforma.models import Usuario

    atomica = transaction.atomic()
    atomica.__enter__()
    usuario = Usuario.objects.create(nombre="QA", email="qa@unergy.io", rol="admin", activo=True)
    yield {"usuario": usuario}
    transaction.set_rollback(True)
    atomica.__exit__(None, None, None)


def _crear(datos_usuario, cuerpo, querystring=""):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.clientes.views import ClienteViewSet

    peticion = APIRequestFactory().post(
        f"/api/v1/clientes{querystring}", cuerpo, format="json"
    )
    force_authenticate(peticion, user=UsuarioAutenticado(datos_usuario["usuario"]))
    respuesta = ClienteViewSet.as_view({"post": "create"})(peticion)
    respuesta.render()
    return respuesta


def _editar(datos_usuario, cliente_id, cuerpo, querystring=""):
    from rest_framework.test import APIRequestFactory, force_authenticate

    from api.authentication import UsuarioAutenticado
    from api.v1.clientes.views import ClienteViewSet

    peticion = APIRequestFactory().patch(
        f"/api/v1/clientes/{cliente_id}{querystring}", cuerpo, format="json"
    )
    force_authenticate(peticion, user=UsuarioAutenticado(datos_usuario["usuario"]))
    respuesta = ClienteViewSet.as_view({"patch": "partial_update"})(peticion, pk=cliente_id)
    respuesta.render()
    return respuesta


def _cliente(**kw):
    from apps.clientes.models import Cliente

    kw.setdefault("razon_social_nombre", "Cliente")
    return Cliente.objects.create(**kw)


def _contactos_de(cliente_id):
    from apps.clientes.models import Contacto

    return list(Contacto.objects.filter(cliente_id=cliente_id).order_by("email", "tipo"))


# ── Nombre parecido ──────────────────────────────────────────────────────────

def test_crear_cliente_con_nombre_parecido_da_409_estructurado(datos):
    """El 409 lleva `candidato_id`/`candidato_nombre` porque el frontend los usa
    para ofrecer "crear de todos modos", no solo para mostrar un mensaje."""
    _cliente(razon_social_nombre="Quantum Energy Ingenieria S.A.S.")

    respuesta = _crear(datos, {"razon_social_nombre": "Quantum"})

    assert respuesta.status_code == 409, respuesta.data
    detalle = respuesta.data["detail"]
    assert detalle["duplicado_nombre"] is True
    assert detalle["candidato_nombre"] == "Quantum Energy Ingenieria S.A.S."


def test_crear_forzado_permite_el_nombre_parecido(datos):
    from apps.clientes.models import Cliente

    _cliente(razon_social_nombre="Quantum Energy Ingenieria S.A.S.")

    respuesta = _crear(datos, {"razon_social_nombre": "Quantum"}, "?forzar=true")

    assert respuesta.status_code == 201, respuesta.data
    assert respuesta.data["razon_social_nombre"] == "Quantum"
    assert Cliente.objects.count() == 2


# ── NIT ──────────────────────────────────────────────────────────────────────

def test_crear_cliente_con_nit_duplicado_lo_explica_en_castellano(datos):
    """Un NIT ya usado se rechaza ANTES de escribir, con el mismo texto que daba
    FastAPI.

    FastAPI respondia 409 con `detail` de texto; aca lo atrapa la validacion de
    DRF, asi que es un 400 -- mejor, porque tampoco llega a tocar la base y
    cubre igual el PATCH. Lo que importa es que el motivo sea legible: el
    validador que DRF armaba solo decia "Los campos nit_cedula deben formar un
    conjunto único", y con eso el usuario no sabe que arreglar.
    """
    _cliente(razon_social_nombre="Cliente Uno", nit_cedula="9001234567")

    respuesta = _crear(
        datos,
        {"razon_social_nombre": "Cliente Totalmente Distinto", "nit_cedula": "900123456-7"},
        "?forzar=true",
    )

    assert respuesta.status_code == 400, respuesta.data
    assert respuesta.data["nit_cedula"] == ["Ya existe un cliente con ese NIT/cédula."]


def test_el_cliente_no_queda_creado_cuando_el_nit_choca(datos):
    from apps.clientes.models import Cliente

    _cliente(razon_social_nombre="Cliente Uno", nit_cedula="9001234567")

    _crear(
        datos,
        {"razon_social_nombre": "Cliente Totalmente Distinto", "nit_cedula": "900123456-7"},
        "?forzar=true",
    )

    assert Cliente.objects.count() == 1


def test_editar_un_cliente_sin_tocarle_el_nit_no_choca_consigo_mismo(datos):
    """El validador por campo excluye la fila que se esta editando; si no, un
    PATCH de cualquier campo se caeria por el NIT que el cliente ya tenia.

    El NIT va escrito de otra forma a proposito: normalizado es el mismo, y
    tiene que reconocerse como el suyo.
    """
    cliente = _cliente(razon_social_nombre="Cliente Uno", nit_cedula="9001234567")

    respuesta = _editar(datos, cliente.id, {
        "razon_social_nombre": "Cliente Uno S.A.S.",
        "nit_cedula": "900-123.456 7",
    }, "?forzar=true")

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["razon_social_nombre"] == "Cliente Uno S.A.S."


# ── NIT normalizado ──────────────────────────────────────────────────────────

def test_el_nit_se_guarda_solo_con_digitos(datos):
    from apps.clientes.models import Cliente

    respuesta = _crear(datos, {
        "razon_social_nombre": "Cliente Con Puntos",
        "nit_cedula": "900.123.456-7",
    })

    assert respuesta.status_code == 201, respuesta.data
    assert Cliente.objects.get(pk=respuesta.data["id"]).nit_cedula == "9001234567"


def test_el_mismo_nit_escrito_distinto_choca(datos):
    """El hueco que esto cierra: el UNIQUE compara texto, asi que
    "900.123.456-7" y "900123456-7" entraban como dos clientes distintos."""
    _cliente(razon_social_nombre="Cliente Uno", nit_cedula="9001234567")

    respuesta = _crear(datos, {
        "razon_social_nombre": "Otro Cliente Cualquiera",
        "nit_cedula": "900.123.456-7",
    }, "?forzar=true")

    assert respuesta.status_code == 400, respuesta.data
    assert respuesta.data["nit_cedula"] == ["Ya existe un cliente con ese NIT/cédula."]


def test_dos_clientes_sin_nit_no_chocan_entre_si(datos):
    """En Postgres los NULL no colisionan, y la mayoria de los clientes no
    tienen NIT cargado: si el validador tratara el vacio como un valor, el alta
    mas comun quedaria bloqueada."""
    from apps.clientes.models import Cliente

    _cliente(razon_social_nombre="Sin NIT Uno")

    respuesta = _crear(
        datos,
        {"razon_social_nombre": "Sin NIT Dos", "nit_cedula": ""},
        "?forzar=true",
    )

    assert respuesta.status_code == 201, respuesta.data
    assert Cliente.objects.get(pk=respuesta.data["id"]).nit_cedula is None


# ── Nombre parecido al EDITAR ────────────────────────────────────────────────

def test_renombrar_un_cliente_para_dejarlo_igual_a_otro_avisa(datos):
    """Antes solo se revisaba al crear: la proteccion se saltaba con dos clics
    --crear con un nombre cualquiera y renombrarlo despues."""
    _cliente(razon_social_nombre="Quantum Energy Ingenieria S.A.S.")
    otro = _cliente(razon_social_nombre="Cliente Sin Relacion")

    respuesta = _editar(datos, otro.id, {"razon_social_nombre": "Quantum"})

    assert respuesta.status_code == 409, respuesta.data
    assert respuesta.data["detail"]["candidato_nombre"] == "Quantum Energy Ingenieria S.A.S."


def test_al_editar_tambien_se_puede_forzar(datos):
    from apps.clientes.models import Cliente

    _cliente(razon_social_nombre="Quantum Energy Ingenieria S.A.S.")
    otro = _cliente(razon_social_nombre="Cliente Sin Relacion")

    respuesta = _editar(datos, otro.id, {"razon_social_nombre": "Quantum"}, "?forzar=true")

    assert respuesta.status_code == 200, respuesta.data
    assert Cliente.objects.get(pk=otro.id).razon_social_nombre == "Quantum"


def test_editar_otro_campo_no_dispara_el_aviso_de_nombre(datos):
    """Un PATCH que no toca el nombre no tiene por que revisarlo: si lo
    revisara, el cliente se compararia contra si mismo o contra un parecido
    viejo y no se podria editar ni el telefono."""
    _cliente(razon_social_nombre="Quantum Energy Ingenieria S.A.S.")
    cliente = _cliente(razon_social_nombre="Quantum")

    respuesta = _editar(datos, cliente.id, {"ciudad": "Medellín"})

    assert respuesta.status_code == 200, respuesta.data
    assert respuesta.data["ciudad"] == "Medellín"


def test_guardar_un_cliente_con_su_mismo_nombre_no_avisa(datos):
    """Se excluye a si mismo: sin `excluir_id`, todo cliente seria su propio
    duplicado y no se podria guardar el formulario sin cambiarle el nombre."""
    cliente = _cliente(razon_social_nombre="Quantum Energy Ingenieria S.A.S.")

    respuesta = _editar(datos, cliente.id, {
        "razon_social_nombre": "Quantum Energy Ingenieria S.A.S.",
        "ciudad": "Bogotá",
    })

    assert respuesta.status_code == 200, respuesta.data


# ── Contactos ────────────────────────────────────────────────────────────────

def test_dos_contactos_iguales_no_tumban_el_alta(datos):
    """El caso real: el formulario deja agregar dos renglones con el mismo
    correo y el mismo tipo, y el UNIQUE de `contactos` respondia con un 500."""
    respuesta = _crear(datos, {
        "razon_social_nombre": "Cliente Con Contactos Repetidos",
        "contactos": [
            {"nombre": "Ana", "email": "ana@x.com", "tipo": "comercial"},
            {"nombre": "Ana otra vez", "email": "ana@x.com", "tipo": "comercial"},
        ],
    })

    assert respuesta.status_code == 201, respuesta.data
    contactos = _contactos_de(respuesta.data["id"])
    assert len(contactos) == 1
    # Se queda el primero, no el ultimo: es el que el usuario escribio primero.
    assert contactos[0].nombre == "Ana"


def test_el_mismo_correo_en_dos_tipos_distintos_si_se_guarda(datos):
    """La misma persona puede ser el contacto comercial y el de CGM: el UNIQUE
    es por (cliente, email, tipo), y el filtro de repetidos no debe pasarse."""
    respuesta = _crear(datos, {
        "razon_social_nombre": "Cliente Dos Roles",
        "contactos": [
            {"nombre": "Ana", "email": "ana@x.com", "tipo": "comercial"},
            {"nombre": "Ana", "email": "ana@x.com", "tipo": "cgm"},
        ],
    })

    assert respuesta.status_code == 201, respuesta.data
    assert [c.tipo for c in _contactos_de(respuesta.data["id"])] == ["cgm", "comercial"]


def test_el_mismo_correo_con_mayusculas_cuenta_como_repetido(datos):
    """El serializer normaliza el correo antes de validar, asi que el filtro de
    repetidos ve "Ana@X.com" y "ana@x.com" como el mismo -- si no, el UNIQUE los
    dejaba pasar como dos contactos distintos."""
    respuesta = _crear(datos, {
        "razon_social_nombre": "Cliente Correo Con Mayusculas",
        "contactos": [
            {"nombre": "Ana", "email": "Ana@X.com", "tipo": "comercial"},
            {"nombre": "Ana", "email": "ana@x.com", "tipo": "comercial"},
        ],
    })

    assert respuesta.status_code == 201, respuesta.data
    contactos = _contactos_de(respuesta.data["id"])
    assert len(contactos) == 1
    assert contactos[0].email == "ana@x.com"


def test_si_falla_el_insert_de_contactos_no_queda_cliente_a_medias(datos, monkeypatch):
    """La atomicidad, probada por el unico camino que la puede romper de verdad.

    Sin la transaccion, un fallo aca dejaba el cliente creado y devolvia un
    error: el usuario reintentaba y chocaba con el NIT de su propio intento
    anterior.
    """
    from django.db import IntegrityError

    from apps.clientes.models import Cliente, Contacto

    def reventar(*_a, **_k):
        raise IntegrityError("UNIQUE constraint failed: contactos.email")

    monkeypatch.setattr(Contacto.objects, "bulk_create", reventar)

    respuesta = _crear(datos, {
        "razon_social_nombre": "Cliente Que No Debe Quedar",
        "nit_cedula": "901000000-1",
        "contactos": [{"nombre": "Ana", "email": "ana@x.com", "tipo": "comercial"}],
    })

    assert respuesta.status_code == 409, respuesta.data
    assert not Cliente.objects.filter(nit_cedula="901000000-1").exists()
