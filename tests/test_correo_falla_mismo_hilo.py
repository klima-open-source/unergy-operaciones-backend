"""Los avisos de una misma falla (registrada, programada, cerrada...) llegan al
mismo hilo del correo: asunto fijo y una referencia común (`In-Reply-To` /
`References`), cada correo con su propio `Message-ID`."""
from apps.comun.integraciones import email_service


def _enviar(monkeypatch, estado_codigo, codigo="FAL-2026-11867", to=("a@test.com",)):
    enviados = []
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(email_service, "_smtp_send", lambda msg, recipients: enviados.append(msg))
    monkeypatch.setattr(email_service, "_log_envio", lambda **kw: None)
    email_service.send_falla_notification_email(
        to_emails=list(to),
        codigo_falla=codigo,
        proyecto_nombre="Minigranja Solar Baraya",
        descripcion="Mantenimiento programado por el Operador de red",
        estado_codigo=estado_codigo,
        estado_etiqueta=estado_codigo,
        prioridad_etiqueta="Crítica",
        fecha_identificacion="2026-10-14",
        registrado_por="Alguien",
    )
    return enviados


def test_los_avisos_de_una_falla_comparten_asunto_y_referencia(monkeypatch):
    programado = _enviar(monkeypatch, "programado")[0]
    cerrada = _enviar(monkeypatch, "cerrada")[0]

    assert programado["Subject"] == cerrada["Subject"]
    assert programado["References"] == cerrada["References"]
    assert programado["In-Reply-To"] == programado["References"]
    assert "FAL-2026-11867" in programado["References"]
    assert programado["Message-ID"] != cerrada["Message-ID"]


def test_cada_destinatario_recibe_un_message_id_propio(monkeypatch):
    msgs = _enviar(monkeypatch, "abierta", to=("a@test.com", "b@test.com"))

    assert len({m["Message-ID"] for m in msgs}) == 2
    assert len({m["References"] for m in msgs}) == 1


def test_fallas_distintas_no_comparten_hilo(monkeypatch):
    una = _enviar(monkeypatch, "abierta", codigo="FAL-2026-00001")[0]
    otra = _enviar(monkeypatch, "abierta", codigo="FAL-2026-00002")[0]

    assert una["References"] != otra["References"]
    assert una["Subject"] != otra["Subject"]
