"""El correo de falla al cliente NO lleva enlace a la plataforma. Y `FRONTEND_URL`
--que sigue vivo para otro correo-- no puede apuntar a localhost.

Dos cosas distintas que comparten la misma variable, y por eso viven juntas acá.

── 1. El correo de falla no debe ofrecer un botón ────────────────────────────

Tenía un botón "Ver detalle de la falla" apuntando a `/fallas/{id}` de la
plataforma interna. El cliente **no puede abrirlo**: no hay rol de cliente
(admin, operaciones, monitoreo, liquidaciones, comercial, coordinador, tecnico
son los siete que existen, todos internos) y el guard global del frontend lo
manda al login, donde se queda.

Antes del 2026-09-07 era peor: la URL era `http://localhost:5173` --el default
de desarrollo, con el puerto de antes de Nuxt-- así que el botón no llevaba a
ninguna parte. Corregir el dominio lo dejó llevando a un login inaccesible, que
sigue sin servirle a nadie. Decisión de la usuaria: quitar el botón. El correo
conserva código, proyecto, estado, prioridad y descripción, que es lo que el
cliente necesita.

Para terceros que sí necesiten consultar existe `GET /fallas/por-proyecto` con
API Key -- una API para integrar, no una página.

── 2. `FRONTEND_URL` sigue importando, para el correo de recuperar contraseña ─

`email_service.py` arma `f"{FRONTEND_URL}/reset-password/{token}"`, y ese sí va
a un usuario interno CON cuenta. Con el default viejo, quien olvidaba su
contraseña recibía un enlace a localhost. Ese default vive en el código y el
`.env` de producción no lo trae (ver el docstring de apps/comun/config.py), así
que el valor de acá es el que usa producción.
"""
import inspect

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


# ── El correo de falla ────────────────────────────────────────────────────────

def test_el_correo_de_falla_no_lleva_ningun_enlace():
    """Un cliente no puede pasar del login, así que cualquier enlace a la
    plataforma es un botón que no funciona."""
    from app.services.email_service import send_falla_notification_email

    fuente = inspect.getsource(send_falla_notification_email)

    assert "<a href" not in fuente, (
        "El correo de falla volvió a tener un enlace. El cliente no tiene cuenta "
        "en la plataforma: el guard lo manda al login y ahí se queda. Si hace "
        "falta que consulte, el camino es GET /fallas/por-proyecto con API Key, "
        "o una vista pública nueva."
    )
    assert "frontend_url" not in fuente, (
        "Volvió el parámetro `frontend_url` a este correo. Se quitó junto con el "
        "botón; si vuelve, alguien está armando otra vez un enlace."
    )


def test_el_correo_de_falla_sigue_registrando_su_proyecto():
    """`proyecto_id` NO era del enlace: va al log de auditoría del envío
    (`_log_envio`). Se quitó por error al sacar el botón y se restituyó -- este
    test es para no repetirlo."""
    from app.services.email_service import send_falla_notification_email

    firma = inspect.signature(send_falla_notification_email).parameters

    assert "proyecto_id" in firma
    assert "proyecto_id=proyecto_id" in inspect.getsource(send_falla_notification_email)


# ── FRONTEND_URL, para el correo de recuperar contraseña ─────────────────────

def test_el_default_de_frontend_url_es_produccion_no_localhost():
    from apps.comun.config import DEFECTOS

    url = DEFECTOS["FRONTEND_URL"]

    assert "localhost" not in url and "127.0.0.1" not in url, (
        f"FRONTEND_URL es {url!r}. Ese valor arma el enlace del correo de "
        "recuperar contraseña: con localhost, un usuario interno que olvida su "
        "clave recibe un enlace que no lleva a ninguna parte."
    )
    assert url == PLATAFORMA, url


def test_los_dos_arboles_declaran_el_mismo_default():
    """El correo lo manda código que lee la config de FastAPI; si divergen, el
    enlace depende de quién lo mande."""
    from app.core.config import settings as fastapi
    from apps.comun.config import DEFECTOS

    assert fastapi.FRONTEND_URL == DEFECTOS["FRONTEND_URL"]


def test_el_correo_de_reset_arma_su_enlace_sobre_la_ruta_que_existe():
    """`/reset-password/{token}` es una ruta real del frontend
    (app/pages/reset-password/[token]). Es el ÚNICO correo que debe llevar a la
    plataforma, porque va a alguien que sí tiene cuenta."""
    from app.services.email_service import send_reset_password_email

    fuente = inspect.getsource(send_reset_password_email)

    assert 'f"{settings.FRONTEND_URL}/reset-password/{token}"' in fuente, (
        "cambió la forma del enlace de reset: verificar que la ruta nueva exista "
        "en el frontend antes de actualizar este test."
    )
