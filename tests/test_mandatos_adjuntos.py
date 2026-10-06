"""Expansión de adjuntos: un ZIP de la revisoría rinde los PDFs de adentro."""
import io
import zipfile

from apps.mandatos.services.adjuntos import expandir_adjuntos


def _zip(nombres: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, c in nombres.items():
            zf.writestr(n, c)
    return buf.getvalue()


def test_un_pdf_suelto_pasa_igual():
    adj = [("CMU1-Mandato-Costos-X.pdf", b"%PDF-1.4")]
    assert expandir_adjuntos(adj) == adj


def test_zip_rinde_sus_pdfs():
    z = _zip({"CMU1-Mandato-Costos-X.pdf": b"%PDF-a",
              "CMU2-Mandato-Costos-Y.pdf": b"%PDF-b"})
    r = dict(expandir_adjuntos([("mandatos.zip", z)]))
    assert sorted(r) == ["CMU1-Mandato-Costos-X.pdf", "CMU2-Mandato-Costos-Y.pdf"]
    assert r["CMU1-Mandato-Costos-X.pdf"] == b"%PDF-a"


def test_zip_descarta_lo_que_no_es_pdf():
    z = _zip({"CMU1-Mandato-Costos-X.pdf": b"%PDF-a", "notas.txt": b"hola"})
    assert [n for n, _ in expandir_adjuntos([("z.zip", z)])] == [
        "CMU1-Mandato-Costos-X.pdf"]


def test_zip_ignora_rutas_de_carpeta_internas():
    """Un ZIP con carpetas adentro debe rendir solo el nombre del archivo."""
    z = _zip({"julio/CMU1-Mandato-Costos-X.pdf": b"%PDF-a"})
    assert [n for n, _ in expandir_adjuntos([("z.zip", z)])] == [
        "CMU1-Mandato-Costos-X.pdf"]


def test_zip_corrupto_no_revienta():
    assert expandir_adjuntos([("roto.zip", b"esto no es un zip")]) == []


def test_mezcla_de_zip_y_pdf_suelto():
    z = _zip({"CMU1-Mandato-Costos-X.pdf": b"%PDF-a"})
    r = expandir_adjuntos([("z.zip", z), ("CMU2-Mandato-Costos-Y.pdf", b"%PDF-b")])
    assert sorted(n for n, _ in r) == [
        "CMU1-Mandato-Costos-X.pdf", "CMU2-Mandato-Costos-Y.pdf"]
