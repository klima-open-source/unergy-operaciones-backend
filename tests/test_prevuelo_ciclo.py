"""Antes de disparar un paso del ciclo, revisar que sus insumos existan.

El 2026-09-24 Jessica corrió Liquidar (dos veces) y Repartir sobre una versión de
reliquidación, y los tres terminaron en FAILURE del lado de la API. No había nada
roto: esa versión no tenía ni archivos del FTP ni facturas de XM —los 1.736
registros de FTP, los 2.263 de liquidación y las 4 facturas de agosto estaban
**todos** en `txf`.

La plataforma tenía cómo saberlo y no lo miró. Lanzó la tarea igual, y el
resultado fue un FAILURE opaco en una lista de tareas, sin decir qué faltaba.

Esto revisa los insumos ANTES y explica qué falta. No es una validación de forma
—eso ya lo hacen los serializers—: es mirar si el trabajo tiene con qué correr.

Las reglas salen de la guía:

  * **Liquidar** (§4.5) requiere los archivos del FTP de esa versión.
  * **Repartir** (§4.6) requiere `readiness.ready_for_distribution`, y cuando es
    `false` la propia API dice por qué en `blockers`.

Se puede saltar con `forzar`. La revisión es para que no se falle por descuido,
no para bloquear a quien sabe lo que hace.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _liquidar():
    from apps.liquidaciones.services.prevuelo import problemas_para_liquidar

    return problemas_para_liquidar


def _repartir():
    from apps.liquidaciones.services.prevuelo import problemas_para_repartir

    return problemas_para_repartir


# ── Liquidar ────────────────────────────────────────────────────────────────

def test_con_archivos_del_ftp_se_puede_liquidar():
    assert _liquidar()(filas_ftp=1736, version="txf") == []


def test_sin_archivos_del_ftp_no_se_liquida():
    problemas = _liquidar()(filas_ftp=0, version="tx3")
    assert len(problemas) == 1
    assert "tx3" in problemas[0]
    assert "FTP" in problemas[0]


def test_el_aviso_dice_que_hacer_cuando_es_una_reliquidacion():
    """Sin esta pista, el mensaje dice qué falta pero no cómo salir del paso."""
    problemas = _liquidar()(filas_ftp=0, version="tx3", filas_ftp_inicial=1736)
    assert "Reliquidar" in problemas[0]


def test_si_tampoco_hay_nada_en_txf_no_se_sugiere_reliquidar():
    """Si el mes entero está vacío, el problema es el FTP, no la versión."""
    problemas = _liquidar()(filas_ftp=0, version="txf", filas_ftp_inicial=0)
    assert "Reliquidar" not in problemas[0]


# ── Repartir ────────────────────────────────────────────────────────────────

def test_con_las_facturas_listas_se_puede_repartir():
    assert _repartir()(readiness={"ready_for_distribution": True}, version="txf") == []


def test_sin_facturas_no_se_reparte_y_se_dice_por_que():
    """La API ya explica qué falta: se muestra tal cual, no se reescribe."""
    problemas = _repartir()(
        readiness={
            "ready_for_distribution": False,
            "blockers": ["No hay facturas XM cargadas para este período y versión."],
        },
        version="tx3",
    )
    assert any("No hay facturas XM cargadas" in p for p in problemas)


def test_se_listan_todos_los_bloqueos_no_solo_el_primero():
    problemas = _repartir()(
        readiness={"ready_for_distribution": False,
                   "blockers": ["falta A", "falta B"]},
        version="txf",
    )
    assert len(problemas) == 2


def test_sin_bloqueos_explicitos_igual_se_avisa():
    """`ready_for_distribution: false` sin `blockers` no puede pasar callado."""
    problemas = _repartir()(readiness={"ready_for_distribution": False}, version="txf")
    assert len(problemas) == 1
    assert "txf" in problemas[0]


def test_una_respuesta_vacia_de_la_api_no_bloquea():
    """Si la API no devuelve `readiness`, no se inventa un problema: se deja
    correr y que falle allá con su propio error."""
    assert _repartir()(readiness=None, version="txf") == []
    assert _repartir()(readiness={}, version="txf") == []
