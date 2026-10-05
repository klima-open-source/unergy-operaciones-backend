import email as email_pkg

from apps.comun.integraciones import email_service


def test_adjunto_con_tildes_conserva_nombre_de_archivo(monkeypatch):
    # Caso real 2026-07-10: "COX ENERGY GENERACIÓN..." y "CGM Ingeniería"
    # llegaban con el adjunto sin nombre ni ícono en Gmail -- el filename se
    # interpolaba a mano en el header, sin la codificación RFC 2231 que
    # necesitan los nombres con tildes/ñ. La corrupción solo aparece al
    # serializar el mensaje a bytes (lo que de verdad viaja por SMTP) y
    # volverlo a parsear -- por eso el test hace ese viaje completo, no solo
    # inspecciona el objeto Message en memoria (eso pasaría igual con el bug).
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")

    capturado = {}

    def _fake_smtp_send(msg, recipients):
        capturado["msg"] = msg

    monkeypatch.setattr(email_service, "_smtp_send", _fake_smtp_send)
    monkeypatch.setattr(email_service, "_log_envio", lambda **kw: None)

    # El slug real conserva tildes/ñ (str.isalnum() las trata como alfanuméricas
    # en Python) -- reproducir eso aquí, no un nombre ya "limpio" a mano.
    filename = "cgm-report-2026-07-09-cgm_ingeniería_s_a_s.xlsx"
    email_service.send_reporte_cgm_email(
        to_emails=["destino@example.com"],
        excel_bytes=b"contenido-fake-xlsx",
        filename=filename,
        fecha_str="2026-07-09",
        destinatario_nombre="CGM Ingeniería S.A.S",
    )

    # Viaje completo: serializar a bytes (como hace smtplib) y re-parsear
    # (como hace el servidor/cliente que lo recibe).
    reparsed = email_pkg.message_from_bytes(capturado["msg"].as_bytes())
    adjunto = next(p for p in reparsed.walk() if p.get_filename() == filename)
    assert adjunto.get_filename() == filename
