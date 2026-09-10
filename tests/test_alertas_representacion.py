"""Alertas de aniversario de Representación/CGM: de dónde sale el IPC.

El servicio traía las tasas en un dict escrito a mano
(`{2023: 0.0928, 2024: 0.052, 2025: 0.051}`) con un `IPC_POR_DEFECTO = 0.051`
para todo lo que cayera fuera. Dos consecuencias que nadie veía:

  * cualquier aniversario de 2027 en adelante se indexaba al 5,1% inventado, sin
    decir que era un supuesto;
  * la tarifa se capitalizaba con UNA sola tasa elevada a la cantidad de
    aniversarios (`tarifa * (1 + ipc) ** numero`), así que un contrato de 2023
    —cuyos años reales fueron 9,28%, 5,20% y 5,10%— salía calculado como 5,10³.

Ahora las tasas salen de `om_ipc_tasas`, que es la única con mantenimiento
(la tarea `om.revisar_ipc_del_anio` le crea la fila cada 1-enero y hay pantalla
para confirmarla). Esa tabla indexa por año de APLICACIÓN: la fila 2026 guarda
el IPC de dic-2025, que es el que indexa un aniversario de 2026. De ahí que la
búsqueda sea directa y el año que se MUESTRA sea el anterior.

Sin `pytest-django`: el fixture arma sqlite en memoria con `create_test_db`,
igual que `tests/test_fallas_django.py` — ver su docstring para el detalle de
por qué hay que invalidar el cache del `ConnectionHandler`.
"""
from datetime import date

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


# Los tres años con tasa real, por año de APLICACIÓN (como los guarda la tabla).
TASAS = {2024: 0.0928, 2025: 0.052, 2026: 0.051}


# ── De qué año sale la tasa ───────────────────────────────────────────────────

def test_ipc_de_toma_la_tasa_del_anio_del_aniversario():
    """La fila 2026 es la que indexa el aniversario de 2026 (búsqueda directa)."""
    from apps.contratos.services.alertas_representacion import ipc_de

    assert ipc_de(2026, TASAS) == (0.051, 2025)


def test_ipc_de_muestra_el_anio_anterior():
    """El número es el IPC de dic-2025 aunque se aplique en 2026: se etiqueta 2025."""
    from apps.contratos.services.alertas_representacion import ipc_de

    _, anio_mostrado = ipc_de(2026, TASAS)
    assert anio_mostrado == 2025


def test_ipc_de_es_none_si_nadie_confirmo_la_tasa():
    """Antes devolvía 0.051 inventado; ahora dice que no sabe."""
    from apps.contratos.services.alertas_representacion import ipc_de

    assert ipc_de(2027, TASAS) == (None, 2026)


# ── Encadenado por año ────────────────────────────────────────────────────────

def test_factor_encadena_la_tasa_de_cada_anio():
    """Tres aniversarios (2024, 2025, 2026) → producto de las tres tasas."""
    from apps.contratos.services.alertas_representacion import factor_ipc

    factor, faltantes = factor_ipc(2026, 3, TASAS)
    assert factor == pytest.approx(1.0928 * 1.052 * 1.051)
    assert faltantes == []


def test_factor_reporta_los_anios_sin_tasa():
    from apps.contratos.services.alertas_representacion import factor_ipc

    factor, faltantes = factor_ipc(2026, 3, {2026: 0.051})
    assert factor == pytest.approx(1.051)
    assert faltantes == [2024, 2025]


def test_tarifa_usa_cada_tasa_y_no_una_elevada():
    """5,0 con años 9,28/5,20/5,10 da 6,0413 — no 5,8047 (que es 5·1,051³)."""
    from apps.contratos.services.alertas_representacion import tarifa_indexada

    assert tarifa_indexada(5.0, 2026, 3, TASAS) == 6.0413


def test_tarifa_es_none_si_falta_la_tasa_del_propio_aniversario():
    """La tarifa nueva ES la del aniversario que se avisa: sin esa tasa no hay
    número que dar, aunque se conozcan las de años anteriores."""
    from apps.contratos.services.alertas_representacion import tarifa_indexada

    assert tarifa_indexada(5.0, 2030, 2, TASAS) is None   # ni 2029 ni 2030
    assert tarifa_indexada(5.0, 2030, 6, TASAS) is None   # 2025/2026 sí, 2030 no


def test_tarifa_sale_aunque_falten_anios_viejos():
    """Con la tasa del aniversario cargada el número se puede dar; los años
    viejos que falten se avisan aparte como proyección parcial."""
    from apps.contratos.services.alertas_representacion import tarifa_indexada

    assert tarifa_indexada(5.0, 2026, 3, {2026: 0.051}) == 5.255


def test_tarifa_es_none_sin_tarifa_base():
    from apps.contratos.services.alertas_representacion import tarifa_indexada

    assert tarifa_indexada(None, 2026, 1, TASAS) is None
    assert tarifa_indexada(0, 2026, 1, TASAS) is None


# ── Lo que dice el correo ─────────────────────────────────────────────────────

CONTRATO = {
    "firma": None,
    "proyecto": "Minigranja Solar Uruaco",
    "inversionista": "Patrimonio Autónomo",
    "tarifa_cgm": 5.0,
    "tarifa_representacion": 5.0,
}


def _html(anio: int, numero: int, tasas: dict[int, float]) -> str:
    from datetime import date

    from apps.contratos.services.alertas_representacion import construir_html

    return construir_html(CONTRATO, date(anio, 6, 15), numero, 30, tasas)


def test_correo_etiqueta_el_ipc_con_el_anio_anterior():
    html = _html(2026, 3, TASAS)
    assert "5.10%" in html
    assert "IPC dic 2025" in html


def test_correo_avisa_igual_sin_tasa_pero_no_inventa_tarifa():
    """La alerta del aniversario sale; la tarifa se omite y se dice por qué."""
    html = _html(2030, 6, TASAS)
    assert "pendiente de confirmación" in html
    assert "Nueva tarifa" not in html
    assert "se cumple el aniversario" in html


def test_correo_marca_la_proyeccion_parcial():
    """Con solo una de las tres tasas, el número sale pero se avisa que es parcial."""
    html = _html(2026, 3, {2026: 0.051})
    assert "Nueva tarifa" in html
    assert "parcial" in html.lower()
    assert "2024" in html and "2025" in html


def test_correo_completo_no_habla_de_parcial():
    html = _html(2026, 3, TASAS)
    assert "parcial" not in html.lower()


# ── Aniversarios ──────────────────────────────────────────────────────────────

def test_29_de_febrero_cae_al_28_en_anio_no_bisiesto():
    from datetime import date

    from apps.contratos.services.alertas_representacion import proximo_aniversario

    aniversario, numero = proximo_aniversario(date(2024, 2, 29), date(2025, 1, 1))
    assert (aniversario, numero) == (date(2025, 2, 28), 1)


# ── La lectura de la tabla ────────────────────────────────────────────────────

def test_solo_entran_las_tasas_confirmadas():
    """La tarea del 1-enero crea la fila en 0,0 con `confirmado=False`. Leerla sin
    filtrar aplicaría 0% de indexación en silencio, que es peor que no saber."""
    from django.db import transaction

    from apps.contratos.services.alertas_representacion import tasas_ipc_confirmadas
    from apps.om.models import OmIpcTasa

    with transaction.atomic():
        OmIpcTasa.objects.create(año=2025, tasa="0.052000", confirmado=True, fuente="DANE")
        OmIpcTasa.objects.create(año=2026, tasa="0.051000", confirmado=True, fuente="DANE")
        OmIpcTasa.objects.create(año=2027, tasa="0.000000", confirmado=False,
                                 fuente="pendiente_confirmacion")

        assert tasas_ipc_confirmadas() == {2025: 0.052, 2026: 0.051}

        transaction.set_rollback(True)


# ── Ventana de aviso e idempotencia ───────────────────────────────────────────
#
# Antes el disparo era `dias not in (30, 15)`: coincidencia EXACTA. Dos agujeros
# que no se podían tapar por separado —pasar a `<=` sin registro manda el correo
# los 30 días seguidos—, así que la ventana y el libro de enviados van juntos:
#
#   * una corrida perdida (deploy, caída, el worker reiniciando a las 8:00)
#     perdía el aviso para siempre, y un contrato dado de alta a 22 días no
#     disparaba ni 30 ni 15: no avisaba NUNCA;
#   * dos corridas el mismo día mandaban dos correos.

HOY = date(2026, 6, 15)


class _Escenario:
    """Contratos en la base en memoria y el job corrido contra un `hoy` fijo.

    `_enviar` se sustituye porque el SMTP es el único borde que no se puede
    ejercer acá; todo lo demás (la elección de ventana, el libro de enviados,
    el filtro de estado) corre de verdad contra la base.
    """

    def __init__(self, srv, ct, enviados, enviar):
        self._srv, self._ct = srv, ct
        self.enviados = enviados
        self._enviar = enviar
        self.hoy = HOY

    def crear(self, firma: date, estado: str = "vigente", nombre: str = "Planta"):
        return self._ct.ContratoServicio.objects.create(
            servicio_aplica="representacion", estado=estado,
            nombre_proyecto_ref=nombre, fecha_firma_contrato=firma,
            tarifa_cgm="5.000000", tarifa_representacion="5.000000",
        )

    def correr(self) -> int:
        return self._srv.revisar_aniversarios()

    def que_el_envio_falle(self, falla: bool = True) -> None:
        self._enviar.exito = not falla

    def avisos(self) -> list[tuple]:
        return sorted(self._ct.AlertaAniversario.objects
                      .values_list("aniversario", "dias_aviso"))


@pytest.fixture
def esc(monkeypatch):
    from django.db import transaction

    from apps.contratos import models as ct
    from apps.contratos.services import alertas_representacion as srv

    monkeypatch.setenv("SMTP_HOST", "smtp.prueba")
    enviados: list[tuple] = []

    def _falso_enviar(contrato, aniversario, numero, dias, tasas):
        enviados.append((contrato["proyecto"], aniversario, dias))
        return _falso_enviar.exito

    _falso_enviar.exito = True
    monkeypatch.setattr(srv, "_enviar", _falso_enviar)

    with transaction.atomic():
        escenario = _Escenario(srv, ct, enviados, _falso_enviar)
        monkeypatch.setattr(srv, "hoy_col", lambda: escenario.hoy)
        yield escenario
        transaction.set_rollback(True)


def test_avisa_aunque_no_sea_el_dia_exacto_del_umbral(esc):
    """A 22 días dispara la ventana de 30. Con `==` este contrato no avisaba nunca."""
    esc.crear(date(2024, 7, 7))            # aniversario 2026-07-07 → 22 días

    assert esc.correr() == 1
    assert esc.avisos() == [(date(2026, 7, 7), 30)]
    assert esc.enviados[0][2] == 22, "el correo debe decir los días reales, no el umbral"


def test_no_avisa_fuera_de_toda_ventana(esc):
    esc.crear(date(2024, 7, 25))           # aniversario 2026-07-25 → 40 días

    assert esc.correr() == 0
    assert esc.avisos() == []


def test_la_segunda_corrida_del_dia_no_repite_el_correo(esc):
    esc.crear(date(2024, 7, 7))

    assert esc.correr() == 1
    assert esc.correr() == 0
    assert len(esc.enviados) == 1


def test_un_envio_fallido_se_reintenta_en_la_corrida_siguiente(esc):
    """La fila se escribe DESPUÉS del envío: un SMTP caído no consume el aviso."""
    esc.crear(date(2024, 7, 7))

    esc.que_el_envio_falle()
    assert esc.correr() == 0
    assert esc.avisos() == [], "un envío fallido no debe dejar registro"

    esc.que_el_envio_falle(False)
    assert esc.correr() == 1
    assert esc.avisos() == [(date(2026, 7, 7), 30)]


def test_cruzar_30_y_despues_15_son_dos_avisos(esc):
    esc.crear(date(2024, 7, 7))            # aniversario 2026-07-07

    assert esc.correr() == 1               # a 22 días → ventana de 30
    esc.hoy = date(2026, 6, 25)            # a 12 días → ventana de 15
    assert esc.correr() == 1

    assert esc.avisos() == [(date(2026, 7, 7), 15), (date(2026, 7, 7), 30)]


def test_el_aniversario_del_ano_siguiente_vuelve_a_avisar(esc):
    """La llave incluye la fecha del aniversario: si solo fuera (contrato, umbral)
    el aviso saldría una única vez en la vida del contrato."""
    esc.crear(date(2024, 7, 7))

    assert esc.correr() == 1               # aniversario 2026
    esc.hoy = date(2027, 6, 15)
    assert esc.correr() == 1               # aniversario 2027

    assert esc.avisos() == [(date(2026, 7, 7), 30), (date(2027, 7, 7), 30)]


# ── Filtro de estado ──────────────────────────────────────────────────────────

def test_no_avisa_contratos_terminados_ni_vencidos(esc):
    """Un contrato terminado no indexa nada: avisar de su aniversario es ruido."""
    esc.crear(date(2024, 7, 7), estado="terminado", nombre="Terminada")
    esc.crear(date(2024, 7, 7), estado="vencido", nombre="Vencida")

    assert esc.correr() == 0
    assert esc.avisos() == []


def test_avisa_contratos_en_renovacion(esc):
    """En renovación es justo cuando la tarifa nueva importa."""
    esc.crear(date(2024, 7, 7), estado="en_renovacion", nombre="En renovación")

    assert esc.correr() == 1
