"""`manage.py revisar_fuente_generacion_hoy`: cuántas plantas usan el apaño.

El comando existe para responder UNA pregunta antes de quitar el apaño del
medidor: a cuántas plantas les toca hoy. Pocas y quitarlo es trivial; muchas y
el hallazgo es otro -- hay una falla de telemetría de fondo que el apaño lleva
tiempo tapando, y quitarlo primero solo cambiaría un número engañoso por un
tablero lleno de huecos.

Lo que se fija acá es sobre todo que **lea bien las dos respuestas**. Los dos
servicios no usan el mismo idioma para la misma cosa: `generacion_hoy()`
devuelve sus filas bajo `proyectos` y `monitoreo_flota()` bajo `projects`. Leer
la clave equivocada no da error: da una lista vacía y un "ninguna planta usa el
apaño" que es mentira -- exactamente la respuesta tranquilizadora que haría
tomar la decisión equivocada. Por eso hay un caso para cada una.
"""
from io import StringIO

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _fila(pid, nombre, kwh, fuente):
    return {"proyecto_id": pid, "nombre": nombre, "sol_id": pid * 10,
            "kwh_real": kwh, "fuente": fuente}


def _correr(monkeypatch, filas, flota=None, *args):
    from django.core.management import call_command

    from apps.energia.services import solarview_monitoreo as sv

    monkeypatch.setattr(sv, "generacion_hoy", lambda: {"proyectos": filas, "total": 0})
    monkeypatch.setattr(sv, "monitoreo_flota", lambda: {"projects": flota or []})

    salida = StringIO()
    call_command("revisar_fuente_generacion_hoy", *args, stdout=salida, stderr=salida)
    return salida.getvalue()


def test_cuenta_cada_fuente(monkeypatch):
    filas = [
        _fila(1, "Sana A", 100, "inversor"),
        _fila(2, "Sana B", 200, "inversor"),
        _fila(3, "Con apaño", 150, "medidor"),
        _fila(4, "Muda", 0, "sin_dato"),
    ]

    salida = _correr(monkeypatch, filas)

    assert "Plantas en operación con id de SolarView: 4" in salida
    assert "inversores" in salida and "  2  (50%)" in salida
    assert "1 planta(s) muestran hoy el kWh del medidor" in salida


def test_lee_la_clave_correcta_de_generacion_hoy(monkeypatch):
    """`proyectos`, no `projects`. La clave equivocada no da error: da cero
    filas y un 'ninguna planta usa el apaño' que es falso."""
    from django.core.management import call_command

    from apps.energia.services import solarview_monitoreo as sv

    monkeypatch.setattr(sv, "generacion_hoy",
                        lambda: {"proyectos": [_fila(1, "Con apaño", 9, "medidor")]})
    monkeypatch.setattr(sv, "monitoreo_flota", lambda: {"projects": []})

    salida = StringIO()
    call_command("revisar_fuente_generacion_hoy", stdout=salida, stderr=salida)

    assert "1 planta(s)" in salida.getvalue(), "no leyó `proyectos`"


def test_lee_la_clave_correcta_de_la_flota(monkeypatch):
    """`projects` acá, al revés que el otro. Si la leyera mal, el estado saldría
    '?' y se perdería justo lo que distingue una planta caída de una con la
    telemetría rota."""
    filas = [_fila(7, "Con apaño", 50, "medidor")]
    flota = [{"proyecto_id": 7, "status": "online"}]

    salida = _correr(monkeypatch, filas, flota)

    assert "[flota: online]" in salida


def test_nombra_las_plantas_del_apaño(monkeypatch):
    """El punto del comando: que se pueda ir a mirar esas plantas."""
    filas = [
        _fila(3, "Planta Del Medio", 150, "medidor"),
        _fila(9, "Otra Con Apaño", 80, "medidor"),
    ]

    salida = _correr(monkeypatch, filas)

    assert "#3 Planta Del Medio: 150 kWh" in salida
    assert "#9 Otra Con Apaño: 80 kWh" in salida


def test_sin_ninguna_lo_dice_claro(monkeypatch):
    """La respuesta que hace trivial el cambio."""
    filas = [_fila(1, "Sana", 100, "inversor")]

    salida = _correr(monkeypatch, filas)

    assert "Ninguna planta está usando el medidor" in salida
    assert "no cambia nada de lo que se ve hoy" in salida


def test_sin_filas_avisa_en_vez_de_decir_que_todo_esta_bien(monkeypatch):
    """SolarView caído da la misma lista vacía que 'ninguna planta usa el
    apaño'. Confundirlas haría quitar el apaño a ciegas."""
    salida = _correr(monkeypatch, [])

    assert "Sin filas" in salida
    assert "Ninguna planta está usando el medidor" not in salida


def test_si_la_flota_falla_el_reporte_sigue(monkeypatch):
    """El estado es contexto, no el dato. Sin él la cuenta sirve igual."""
    from django.core.management import call_command

    from apps.energia.services import solarview_monitoreo as sv

    def _reventar():
        raise RuntimeError("SolarView no responde")

    monkeypatch.setattr(sv, "generacion_hoy",
                        lambda: {"proyectos": [_fila(1, "Con apaño", 9, "medidor")]})
    monkeypatch.setattr(sv, "monitoreo_flota", _reventar)

    salida = StringIO()
    call_command("revisar_fuente_generacion_hoy", stdout=salida, stderr=salida)

    texto = salida.getvalue()
    assert "Sin estado de flota" in texto
    assert "1 planta(s)" in texto


def test_detalle_lista_todas(monkeypatch):
    filas = [
        _fila(1, "Sana", 100, "inversor"),
        _fila(3, "Con apaño", 150, "medidor"),
    ]

    salida = _correr(monkeypatch, filas, None, "--detalle")

    assert "[INV] #1 Sana" in salida
    assert "[MED] #3 Con apaño" in salida


def test_sin_detalle_no_lista_las_sanas(monkeypatch):
    filas = [_fila(1, "Sana", 100, "inversor")]

    salida = _correr(monkeypatch, filas)

    assert "#1 Sana" not in salida


def test_no_escribe_en_la_base(monkeypatch):
    """Es un reporte. Si alguna vez toca la base, este test lo agarra."""
    import inspect

    from apps.energia.management.commands import revisar_fuente_generacion_hoy

    fuente = inspect.getsource(revisar_fuente_generacion_hoy)

    for prohibido in (".save(", ".create(", ".update(", ".delete("):
        assert prohibido not in fuente, f"el comando escribe: {prohibido}"
