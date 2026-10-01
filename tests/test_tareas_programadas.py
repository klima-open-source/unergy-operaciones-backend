"""El horario y las tareas no se separan, y ningún job del scheduler se pierde.

Al sacar el `BackgroundScheduler` de dentro del proceso web (`app/main.py`) a
Celery, el modo de fallo es que un job simplemente deje de existir: nadie lo
llama, nada falla, y el sondeo de MGS o las alertas de vencimiento dejan de
correr en silencio hasta que alguien nota que hace semanas no llega un correo.

Las dos mitades pueden romperse por separado:

- Una entrada de `HORARIOS` que apunta a una tarea que no existe: beat la
  dispara y el worker la rechaza. No revienta el proceso, solo no pasa nada.
- Una tarea sin entrada en `HORARIOS`: está escrita y registrada, pero nadie la
  programa nunca.

Ninguna de las dos necesita base de datos ni broker.
"""

import os

import pytest


@pytest.fixture(scope="module")
def celery_listo():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    import django

    django.setup()
    from config.celery import app

    # `autodiscover_tasks` es perezoso: sin esto `app.tasks` solo trae las
    # internas de Celery y la prueba pasaría en vacío.
    app.loader.import_default_modules()
    return app


@pytest.fixture(scope="module")
def horarios():
    from config.horarios import HORARIOS

    return HORARIOS


def _nuestras(app) -> set[str]:
    return {n for n in app.tasks if not n.startswith("celery.")}


def test_toda_entrada_del_horario_apunta_a_una_tarea_real(celery_listo, horarios):
    registradas = _nuestras(celery_listo)
    huerfanas = sorted({
        f"{clave} -> {entrada['task']}"
        for clave, entrada in horarios.items()
        if entrada["task"] not in registradas
    })
    assert not huerfanas, (
        "Estas entradas de HORARIOS programan una tarea que no existe. Beat las "
        "dispara y el worker las rechaza, así que el job no corre y nada "
        "falla:\n  " + "\n  ".join(huerfanas)
    )


def test_toda_tarea_registrada_tiene_su_franja(celery_listo, horarios):
    programadas = {entrada["task"] for entrada in horarios.values()}
    sin_franja = sorted(_nuestras(celery_listo) - programadas)
    assert not sin_franja, (
        "Estas tareas están escritas y registradas pero nadie las programa. Si "
        "es a propósito —se dispara a mano desde un endpoint— quita el "
        "`@shared_task` o documéntalo acá:\n  " + "\n  ".join(sin_franja)
    )


def test_el_horario_corre_en_hora_de_bogota(celery_listo):
    """UTC−5 sin horario de verano. En UTC, la corrida de las 3:00am se iría a
    las 10:00pm del día anterior — otro día calendario, y estas tareas trabajan
    sobre 'ayer'."""
    from django.conf import settings

    assert settings.CELERY_TIMEZONE == "America/Bogota"
    assert settings.CELERY_ENABLE_UTC is False
