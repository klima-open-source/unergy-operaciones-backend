"""La forma del enlace de reset -- 2026-08-28.

`send_reset_password_email()` armaba `/reset-password?token=...`, pero las dos
generaciones del frontend declaran el token como **segmento de ruta**: el legacy
en `legacy/src/router/index.js` (`/reset-password/:token`) y el Nuxt en
`app/pages/reset-password/[token]/index.vue`, que lo lee de `route.params.token`.

Con el query no habia ruta que emparejara: la peticion caia en el catch-all
(`app/pages/[...slug].vue`, `redirect: '/dashboard'`), el dashboard exige sesion
y el usuario terminaba en el login. Es decir, el flujo de "olvide mi contrasena"
no devolvia a nadie a su cuenta.

Los tests que ya existian sobre esta funcion
(`test_email_service_log_recipientes.py`) solo miraban que dejara rastro en
`email_envios`: ninguno miraba la URL, que es justo lo que estaba roto.
"""
from email.message import Message

from apps.comun.integraciones import email_service

TOKEN = "0123456789abcdef0123456789abcdef"  # la forma real: uuid4().hex


def _html_del_envio(monkeypatch) -> str:
    """Dispara el envio con SMTP simulado y devuelve el cuerpo HTML."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("FRONTEND_URL", "https://front.example.com")
    monkeypatch.setattr(email_service, "_log_envio", lambda **kw: None)

    capturados: list[Message] = []
    monkeypatch.setattr(email_service, "_smtp_send",
                        lambda msg, recipients: capturados.append(msg))

    email_service.send_reset_password_email(to_email="a@test.com", token=TOKEN)

    assert len(capturados) == 1
    parte = next(p for p in capturados[0].walk()
                 if p.get_content_type() == "text/html")
    return parte.get_payload(decode=True).decode("utf-8")


def test_el_enlace_lleva_el_token_en_la_ruta(monkeypatch):
    html = _html_del_envio(monkeypatch)

    assert f"https://front.example.com/reset-password/{TOKEN}" in html


def test_el_enlace_no_usa_el_query_que_el_front_no_lee(monkeypatch):
    """El bug exacto: `?token=` no empareja ninguna ruta del front."""
    html = _html_del_envio(monkeypatch)

    assert "reset-password?token=" not in html


def test_sin_smtp_la_url_del_log_tiene_la_misma_forma(monkeypatch, capsys):
    """La rama sin SMTP imprime la URL en los logs y es la que se usa a mano
    cuando el correo no sale: si diverge, se reparte un enlace roto."""
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("FRONTEND_URL", "https://front.example.com")

    email_service.send_reset_password_email(to_email="a@test.com", token=TOKEN)

    salida = capsys.readouterr().out
    assert f"https://front.example.com/reset-password/{TOKEN}" in salida
    assert "reset-password?token=" not in salida
