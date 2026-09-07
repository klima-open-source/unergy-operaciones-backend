"""El enlace del correo al cliente no puede apuntar a localhost.

Bug real (2026-09-07): `FRONTEND_URL` tenia como default
`http://localhost:5173`, y ese valor termina en el boton "Ver detalle de la
falla" del correo que se manda AL CLIENTE cuando se le notifica una falla
(`app/services/email_service.py`). El cliente recibia un enlace que no lleva a
ninguna parte.

Dos defectos encadenados, y el segundo es el interesante:

  1. El default era de desarrollo. Encima con el puerto viejo: venia de antes de
     Nuxt (`nuxt dev` usa 3000, no 5173), asi que no servia ni localmente.

  2. `send_falla_notification_email` **ya degrada bien**: con `frontend_url`
     vacio pone un texto sin enlace en vez del boton. Pero
     `fallas/notificacion.py` pasaba `settings.FRONTEND_URL or
     "http://localhost:5173"` -- siempre verdadero-- asi que esa defensa NUNCA
     podia dispararse. Un fallback puesto "por seguridad" anulaba la proteccion
     que existia una capa mas abajo.

Por que nadie lo veia: del lado de la plataforma no se nota nada. El correo se
manda, el boton se ve bien, y el fallo solo aparece cuando alguien del lado del
cliente le da clic. No hay log, no hay error, no hay pantalla donde mirarlo.

Este test cubre las dos mitades: que el default sea de produccion, y que
`notificacion.py` no reintroduzca un fallback que tape la degradacion.
"""
import inspect
import re

import pytest

pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


PLATAFORMA = "https://operaciones.unergy.io"


def test_el_default_es_produccion_no_localhost():
    """El default vive en el codigo, no en el `.env` (ver el docstring de
    apps/comun/config.py), asi que este valor es el que usa produccion."""
    from apps.comun.config import DEFECTOS

    url = DEFECTOS["FRONTEND_URL"]

    assert "localhost" not in url and "127.0.0.1" not in url, (
        f"FRONTEND_URL es {url!r}. Ese valor va al boton del correo al cliente: "
        "un enlace a localhost no lleva a ninguna parte y no se nota desde la "
        "plataforma."
    )
    assert url == PLATAFORMA, url


def test_los_dos_arboles_declaran_el_mismo_default():
    """`SoleniumClient` y otros servicios portados leen la config de FastAPI, y
    el correo la de Django: si divergen, el enlace depende de quien lo mande."""
    from app.core.config import settings as fastapi
    from apps.comun.config import DEFECTOS

    assert fastapi.FRONTEND_URL == DEFECTOS["FRONTEND_URL"]


def test_la_notificacion_no_tapa_la_degradacion_con_un_fallback():
    """Con `frontend_url` vacio el correo pone un texto sin enlace en vez de un
    boton roto. Un `or "http://..."` al pasarlo hace que eso nunca ocurra."""
    from apps.monitoreo.services.fallas import notificacion

    fuente = inspect.getsource(notificacion)
    linea = re.search(r"frontend_url\s*=\s*(.+?),\s*$", fuente, re.MULTILINE)

    assert linea, "no se encontro el paso de `frontend_url` -- revisar este test"
    assert "or " not in linea.group(1), (
        f"`frontend_url={linea.group(1)}` trae un fallback. Con eso la rama de "
        "email_service que evita el boton roto nunca se ejecuta."
    )
    # Sin los comentarios: el docstring y las notas de este modulo mencionan
    # localhost a proposito, para explicar el bug. Lo que no puede haberlo es el
    # codigo.
    codigo = "\n".join(
        l for l in fuente.splitlines() if not l.lstrip().startswith("#")
    )
    assert "localhost" not in codigo, (
        "notificacion.py tiene una URL de desarrollo en el codigo: no puede "
        "haber localhost en el camino del correo al cliente."
    )


def test_el_correo_arma_el_enlace_sobre_la_ruta_que_existe():
    """`/fallas/{id}` es una ruta real del frontend
    (app/pages/fallas/[id]/index.vue). Se fija el patron para que un cambio de
    ruta no deje el boton apuntando a un 404."""
    from app.services import email_service

    fuente = inspect.getsource(email_service.send_falla_notification_email)

    assert 'f"{frontend_url}/fallas/{falla_id}"' in fuente, (
        "cambio la forma del enlace: verificar que la ruta nueva exista en el "
        "frontend antes de actualizar este test."
    )
