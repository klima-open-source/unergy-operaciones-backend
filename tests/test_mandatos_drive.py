"""Subida de los PDF de mandatos a Drive (`apps/mandatos/services/drive.py`).

Drive se simula: ningún test sale a la red.
"""
import pytest

pytest.importorskip("googleapiclient", reason="requiere google-api-python-client (uv sync)")


class _Llamada:
    def __init__(self, respuesta):
        self._respuesta = respuesta

    def execute(self):
        return self._respuesta


class _Archivos:
    def __init__(self):
        self.creados = []

    def list(self, **kw):
        return _Llamada({"files": []})  # la subcarpeta no existe: se crea

    def create(self, body, **kw):
        self.creados.append(body)
        es_carpeta = body.get("mimeType") == "application/vnd.google-apps.folder"
        return _Llamada({"id": "carpeta-1"} if es_carpeta else
                        {"id": "pdf-1", "webViewLink": "https://drive/pdf-1"})


class _Drive:
    def __init__(self):
        self.archivos = _Archivos()

    def files(self):
        return self.archivos


def test_sube_el_pdf_a_la_subcarpeta_del_periodo(monkeypatch):
    from apps.comun import drive_evidencia
    from apps.mandatos.services import drive

    falso = _Drive()
    monkeypatch.setattr(drive_evidencia, "servicio", lambda: falso)

    assert drive.subir_pdf(b"%PDF", "CMU123.pdf", "2026-09-firmado") == {
        "id": "pdf-1", "url": "https://drive/pdf-1",
    }
    carpeta, pdf = falso.archivos.creados
    assert carpeta["name"] == "2026-09-firmado"
    assert carpeta["parents"] == [drive.DRIVE_MANDATOS_FOLDER_ID]
    assert pdf == {"name": "CMU123.pdf", "parents": ["carpeta-1"]}


def test_sin_drive_configurado_levanta_un_error_propio(monkeypatch):
    from apps.comun.drive_evidencia import DriveNoConfigurado
    from apps.mandatos.services import drive

    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)

    with pytest.raises(DriveNoConfigurado):
        drive.subir_pdf(b"%PDF", "CMU123.pdf", "2026-09-firmado")
