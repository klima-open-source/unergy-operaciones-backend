"""email_service.py -- normalización email_envios (evento) + email_envio_
destinatarios (auditoría 2026-09-01). `_log_envio()` reemplaza a `_log_send()`:
antes cada función loopeaba `for to_email in to_emails: _log_send(...)`,
insertando una fila COMPLETA en email_envios por cada destinatario -- si un
operador tenía 3 contactos configurados, el historial mostraba 3 "Enviado"
idénticos aunque el correo real por SMTP se mandó una sola vez (Reporte CGM).
Ahora cada función llama a `_log_envio()` UNA SOLA VEZ por evento, con la
lista completa de destinatarios reales -- 1 fila en email_envios + N filas en
email_envio_destinatarios."""
from apps.comun.integraciones import email_service


def _sin_smtp_real(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(email_service, "_smtp_send", lambda msg, recipients: None)


def test_send_informe_email_loguea_un_solo_evento_con_todos_los_destinatarios(monkeypatch):
    _sin_smtp_real(monkeypatch)
    llamadas = []
    monkeypatch.setattr(email_service, "_log_envio", lambda **kw: llamadas.append(kw))

    email_service.send_informe_email(
        to_emails=["a@test.com", "b@test.com"],
        cc=["c@test.com"],
        proyecto_nombre="Test", periodo_display="Agosto 2026",
        aprobado_por="Alguien", html_content="<p>hola</p>",
        proyecto_id=42,
    )

    assert len(llamadas) == 1
    destinatarios = llamadas[0]["destinatarios"]
    assert {d["email"] for d in destinatarios} == {"a@test.com", "b@test.com", "c@test.com"}
    assert {d["email"]: d["tipo"] for d in destinatarios} == {
        "a@test.com": "to", "b@test.com": "to", "c@test.com": "cc",
    }
    assert llamadas[0]["proyecto_id"] == 42
    assert llamadas[0]["success"] is True


def test_send_alarm_notification_email_loguea_un_solo_evento(monkeypatch):
    _sin_smtp_real(monkeypatch)
    llamadas = []
    monkeypatch.setattr(email_service, "_log_envio", lambda **kw: llamadas.append(kw))

    email_service.send_alarm_notification_email(
        to_emails=["a@test.com", "b@test.com", "c@test.com"],
        proyecto_nombre="Test", alarm_type="Falla", severity="CRITICAL", details="detalle",
    )

    assert len(llamadas) == 1
    assert {d["email"] for d in llamadas[0]["destinatarios"]} == {"a@test.com", "b@test.com", "c@test.com"}
    assert all(d["tipo"] == "to" for d in llamadas[0]["destinatarios"])


def test_send_reporte_cgm_email_loguea_un_solo_evento_con_fks(monkeypatch):
    _sin_smtp_real(monkeypatch)
    llamadas = []
    monkeypatch.setattr(email_service, "_log_envio", lambda **kw: llamadas.append(kw))

    email_service.send_reporte_cgm_email(
        to_emails=["a@test.com", "b@test.com"],
        excel_bytes=b"fake", filename="reporte.xlsx", fecha_str="2026-08-25",
        destinatario_nombre="Cliente Test", cliente_id=99, operador_red_id=None,
    )

    assert len(llamadas) == 1
    destinatarios = llamadas[0]["destinatarios"]
    assert {d["email"] for d in destinatarios if d["tipo"] == "to"} == {"a@test.com", "b@test.com"}
    assert llamadas[0]["cliente_id"] == 99
    assert llamadas[0]["operador_red_id"] is None


def test_send_falla_notification_email_loguea_un_evento_con_resultado_por_destinatario(monkeypatch):
    """A diferencia de informe/alarma/reporte_cgm, cada destinatario de falla
    recibe un SMTP separado -- puede fallar independiente. El evento sigue
    siendo UNO (una llamada a _log_envio), pero cada destinatario trae su
    propio 'exitoso'."""
    _sin_smtp_real(monkeypatch)
    llamadas = []
    monkeypatch.setattr(email_service, "_log_envio", lambda **kw: llamadas.append(kw))

    envios_smtp = {"a@test.com": True, "b@test.com": False}

    def _smtp_parcial(msg, recipients):
        destino = recipients[0]
        if not envios_smtp[destino]:
            raise RuntimeError("buzón lleno")

    monkeypatch.setattr(email_service, "_smtp_send", _smtp_parcial)

    resultado = email_service.send_falla_notification_email(
        to_emails=["a@test.com", "b@test.com"], codigo_falla="F-001", proyecto_nombre="Test",
        descripcion="desc", estado_etiqueta="Abierta", prioridad_etiqueta="Alta",
        fecha_identificacion="2026-08-25", registrado_por="Alguien",
        proyecto_id=7,
    )

    assert resultado["enviados"] == ["a@test.com"]
    assert len(resultado["errores"]) == 1

    assert len(llamadas) == 1  # un solo evento, no uno por destinatario
    destinatarios = {d["email"]: d for d in llamadas[0]["destinatarios"]}
    assert destinatarios["a@test.com"]["exitoso"] is True
    assert destinatarios["b@test.com"]["exitoso"] is False
    assert "buzón lleno" in destinatarios["b@test.com"]["error"]
    assert llamadas[0]["proyecto_id"] == 7
    assert llamadas[0]["success"] is False  # no todos los destinatarios tuvieron éxito


def test_send_falla_notification_email_evento_exitoso_si_todos_los_destinatarios_llegan(monkeypatch):
    _sin_smtp_real(monkeypatch)
    llamadas = []
    monkeypatch.setattr(email_service, "_log_envio", lambda **kw: llamadas.append(kw))

    email_service.send_falla_notification_email(
        to_emails=["a@test.com"], codigo_falla="F-001", proyecto_nombre="Test",
        descripcion="desc", estado_etiqueta="Abierta", prioridad_etiqueta="Alta",
        fecha_identificacion="2026-08-25", registrado_por="Alguien",
        proyecto_id=7,
    )

    assert len(llamadas) == 1
    assert llamadas[0]["success"] is True


def test_send_reset_password_email_ahora_si_loguea(monkeypatch):
    """Finding #1 de la auditoría original -- era la única función de las 8
    que no llamaba a _log_envio()/_log_send(), un flujo sensible (reset de
    contraseña) sin ningún rastro en email_envios."""
    _sin_smtp_real(monkeypatch)
    llamadas = []
    monkeypatch.setattr(email_service, "_log_envio", lambda **kw: llamadas.append(kw))

    email_service.send_reset_password_email(to_email="a@test.com", token="tok123")

    assert len(llamadas) == 1
    assert llamadas[0]["destinatarios"] == [{"email": "a@test.com", "tipo": "to"}]
    assert llamadas[0]["tipo"] == "reset_password"
    assert llamadas[0]["success"] is True


def test_send_reset_password_email_loguea_el_fallo_tambien(monkeypatch):
    _sin_smtp_real(monkeypatch)
    monkeypatch.setattr(email_service, "_smtp_send", lambda msg, recipients: (_ for _ in ()).throw(RuntimeError("smtp caído")))
    llamadas = []
    monkeypatch.setattr(email_service, "_log_envio", lambda **kw: llamadas.append(kw))

    try:
        email_service.send_reset_password_email(to_email="a@test.com", token="tok123")
    except RuntimeError:
        pass

    assert len(llamadas) == 1
    assert llamadas[0]["success"] is False
    assert "smtp caído" in llamadas[0]["error_msg"]


def test_send_otp_email_ya_no_existe():
    """Finding #3 de la auditoría -- código muerto, cero llamadores en todo
    el repo (nunca existió una ruta/feature de OTP real)."""
    assert not hasattr(email_service, "send_otp_email")


# ── El registro va por la conexión de Django ──────────────────────────────────


class _Cursor:
    def __init__(self, falla=False):
        self.sentencias = []
        self.falla = falla

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        if self.falla:
            raise RuntimeError("la base no responde")
        self.sentencias.append((" ".join(sql.split()), params))

    def fetchone(self):
        return (7,)


def _conexion_falsa(monkeypatch, cursor):
    import contextlib

    import django.db

    monkeypatch.setattr(django.db, "connection", type("C", (), {"cursor": lambda self: cursor})())
    monkeypatch.setattr(django.db.transaction, "atomic", contextlib.nullcontext)


def test_log_envio_escribe_el_evento_y_sus_destinatarios_con_la_conexion_de_django(monkeypatch):
    cursor = _Cursor()
    _conexion_falsa(monkeypatch, cursor)

    email_service._log_envio(
        destinatarios=[{"email": "a@test.com", "tipo": "to"}, {"email": "c@test.com", "tipo": "cc"}],
        subject="Informe", tipo="informe", success=True, proyecto_id=42,
    )

    evento, dest_a, dest_c = cursor.sentencias
    assert evento[0].startswith("INSERT INTO email_envios")
    assert evento[1]["subject"] == "Informe" and evento[1]["proyecto_id"] == 42
    assert dest_a[0].startswith("INSERT INTO email_envio_destinatarios")
    assert (dest_a[1]["envio_id"], dest_a[1]["email"], dest_a[1]["tipo_dest"]) == (7, "a@test.com", "to")
    assert (dest_c[1]["email"], dest_c[1]["tipo_dest"]) == ("c@test.com", "cc")


def test_si_no_se_puede_registrar_el_envio_no_revienta(monkeypatch):
    """Fire-and-forget: el correo ya salió; un fallo al registrarlo no es un
    fallo del envío."""
    _conexion_falsa(monkeypatch, _Cursor(falla=True))

    email_service._log_envio(
        destinatarios=[{"email": "a@test.com"}], subject="x", tipo="informe", success=True,
    )
