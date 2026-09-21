"""Endpoints de Retos Q llamados directamente como funciones, con sqlite en memoria.

Las fechas de los retos de prueba se construyen alrededor de HOY para que los
tests no dependan de la fecha del sistema.

**Y tampoco del día de la semana**, que es la mitad que faltaba: las semanas de
un reto van de lunes a domingo, y `semanas_transcurridas` cuenta como cerrada la
semana cuyo último día ya llegó (`fin_efectivo <= hoy`). Los domingos, entonces,
la semana en curso ya cuenta, y `meta_esperada` sube un séptimo antes de lo que
estos tests esperan: fallaban con "300.0 == 200.0" **solo los domingos**, y con
ellos el despliegue de ese día (encontrado el 2026-09-20).

Por eso el reloj se fija en el MIÉRCOLES de la semana en curso: sigue siendo
"alrededor de hoy", pero cae siempre a mitad de semana, que es el caso que los
tests describen ("2 semanas cerradas, la 3 corriendo"). Se parchea `date` en los
dos módulos por los que entra la fecha; no se toca el cálculo, que es de Retos.
"""
from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
import app.models  # noqa: F401
from app.models.usuarios import Usuario
from app.models.retos import RetoTrimestre, RetoMetrica, RetoValorSemanal
from app.schemas.retos import MetricaCreate, MetricaUpdate, RetoUpdate, ValorSemanalIn
from app.api.v1.retos import (
    actualizar_metrica, actualizar_reto, copiar_metricas, crear_metrica,
    eliminar_metrica, guardar_valor, listar_retos, obtener_reto,
)


@compiles(JSONB, "sqlite")
def _j(e, c, **k):
    return "TEXT"


@compiles(BigInteger, "sqlite")
def _b(e, c, **k):
    return "INTEGER"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[
        Usuario.__table__, RetoTrimestre.__table__,
        RetoMetrica.__table__, RetoValorSemanal.__table__,
    ])
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _miercoles_de_esta_semana() -> date:
    """El reloj de estas pruebas: a mitad de la semana en curso.

    Miércoles y no `date.today()` para que el resultado no cambie según el día
    en que se corran. Ver el docstring del módulo.
    """
    hoy = date.today()
    return hoy - timedelta(days=hoy.weekday()) + timedelta(days=2)


HOY = _miercoles_de_esta_semana()


@pytest.fixture(autouse=True)
def _reloj_a_mitad_de_semana(monkeypatch):
    """Todo el código de Retos ve `HOY` en vez del día real.

    La fecha entra solo por estos dos módulos, y siempre como
    `hoy = hoy or date.today()`.
    """
    class _Fecha(date):
        @classmethod
        def today(cls):
            return HOY

    monkeypatch.setattr("app.services.retos.date", _Fecha)
    monkeypatch.setattr("app.api.v1.retos.date", _Fecha)


def _lunes_hoy() -> date:
    return HOY - timedelta(days=HOY.weekday())


def _reto_alrededor_de_hoy(db, anio=2050, trimestre=1) -> RetoTrimestre:
    """Reto cuyo rango contiene la semana actual (S3 de 7)."""
    lunes = _lunes_hoy()
    r = RetoTrimestre(
        anio=anio, trimestre=trimestre, nombre=f"Retos Q{trimestre} {anio}",
        fecha_inicio=lunes - timedelta(weeks=2),
        fecha_fin=lunes + timedelta(weeks=4, days=6),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _usuario(db) -> Usuario:
    u = Usuario(email="juanjose@unergy.io", nombre="Juan José", rol="admin")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ---------------------------------------------------------------------------
# GET /retos?anio=
# ---------------------------------------------------------------------------

def test_autocrea_los_cuatro_trimestres(db):
    salida = listar_retos(anio=2031, db=db, _=None)

    assert salida.anio == 2031
    assert len(salida.retos) == 4
    assert [r.trimestre for r in salida.retos] == [1, 2, 3, 4]
    assert [r.nombre for r in salida.retos] == [
        "Retos Q1 2031", "Retos Q2 2031", "Retos Q3 2031", "Retos Q4 2031",
    ]
    assert salida.retos[0].fecha_inicio == date(2031, 1, 1)
    assert salida.retos[0].fecha_fin == date(2031, 3, 31)
    assert salida.retos[3].fecha_inicio == date(2031, 10, 1)
    assert salida.retos[3].fecha_fin == date(2031, 12, 31)
    assert {2030, 2031, 2032}.issubset(set(salida.anios_disponibles))
    # Sin métricas todavía
    assert all(r.total_metricas == 0 and r.metricas == [] for r in salida.retos)
    assert all(r.avance_global_pct is None for r in salida.retos)


def test_autocreacion_es_idempotente(db):
    listar_retos(anio=2031, db=db, _=None)
    listar_retos(anio=2031, db=db, _=None)
    assert db.query(RetoTrimestre).filter(RetoTrimestre.anio == 2031).count() == 4


def test_listar_sin_anio_usa_el_actual(db):
    salida = listar_retos(anio=None, db=db, _=None)
    assert salida.anio == date.today().year
    assert len(salida.retos) == 4


# ---------------------------------------------------------------------------
# Métricas
# ---------------------------------------------------------------------------

def test_crear_metrica_asigna_orden_y_defaults(db):
    reto = _reto_alrededor_de_hoy(db)

    m1 = crear_metrica(reto.id, MetricaCreate(nombre="MWh comercializados", unidad="MWh",
                                              meta=1200, decimales=1, responsable="Laura"),
                       db=db, current=None)
    assert m1.id is not None
    assert m1.reto_id == reto.id
    assert m1.orden == 0
    assert m1.tipo_agregacion == "suma"
    assert m1.direccion == "mayor_mejor"
    assert m1.activa is True
    assert m1.estado == "sin_datos"
    assert m1.consolidado is None
    assert m1.semanas_con_dato == 0
    # La serie trae TODAS las semanas del Q, con null donde no hay dato.
    assert len(m1.serie) == 7
    assert [p.semana for p in m1.serie] == list(range(1, 8))
    assert all(p.valor is None for p in m1.serie)

    m2 = crear_metrica(reto.id, MetricaCreate(nombre="Fallas resueltas"), db=db, current=None)
    assert m2.orden == 1


def test_crear_metrica_rechaza_catalogos_invalidos(db):
    reto = _reto_alrededor_de_hoy(db)

    with pytest.raises(HTTPException) as exc:
        crear_metrica(reto.id, MetricaCreate(nombre="X", tipo_agregacion="mediana"),
                      db=db, current=None)
    assert exc.value.status_code == 400
    assert "tipo_agregacion" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        crear_metrica(reto.id, MetricaCreate(nombre="X", direccion="da_igual"),
                      db=db, current=None)
    assert exc.value.status_code == 400


def test_crear_metrica_reto_inexistente(db):
    with pytest.raises(HTTPException) as exc:
        crear_metrica(999, MetricaCreate(nombre="X"), db=db, current=None)
    assert exc.value.status_code == 404


def test_actualizar_y_eliminar_metrica(db):
    reto = _reto_alrededor_de_hoy(db)
    m = crear_metrica(reto.id, MetricaCreate(nombre="Original"), db=db, current=None)

    editada = actualizar_metrica(m.id, MetricaUpdate(nombre="Editada", meta=50,
                                                     tipo_agregacion="promedio", activa=False),
                                 db=db, current=None)
    assert editada.nombre == "Editada"
    assert editada.meta == 50.0
    assert editada.tipo_agregacion == "promedio"
    assert editada.activa is False

    with pytest.raises(HTTPException) as exc:
        actualizar_metrica(m.id, MetricaUpdate(direccion="hacia_arriba"), db=db, current=None)
    assert exc.value.status_code == 400

    guardar_valor(m.id, _lunes_hoy(), ValorSemanalIn(valor=10), db=db, current=None)
    eliminar_metrica(m.id, db=db, current=None)
    assert db.query(RetoMetrica).filter(RetoMetrica.id == m.id).first() is None
    assert db.query(RetoValorSemanal).filter(RetoValorSemanal.metrica_id == m.id).count() == 0

    with pytest.raises(HTTPException) as exc:
        eliminar_metrica(m.id, db=db, current=None)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# PUT valores
# ---------------------------------------------------------------------------

def test_upsert_valor_recalcula_la_metrica(db):
    reto = _reto_alrededor_de_hoy(db)
    usuario = _usuario(db)
    m = crear_metrica(reto.id, MetricaCreate(nombre="MWh", meta=700, tipo_agregacion="suma"),
                      db=db, current=None)
    lunes = _lunes_hoy()

    r1 = guardar_valor(m.id, lunes - timedelta(weeks=2), ValorSemanalIn(valor=100, nota="arranque"),
                       db=db, current=usuario)
    assert r1.consolidado == 100.0
    assert r1.semanas_con_dato == 1
    assert r1.serie[0].valor == 100.0

    r2 = guardar_valor(m.id, lunes - timedelta(weeks=1), ValorSemanalIn(valor=100),
                       db=db, current=usuario)
    assert r2.consolidado == 200.0
    assert r2.semanas_con_dato == 2
    # 2 semanas CERRADAS (la 3 va corriendo) → esperada = 700 * 2/7 = 200 → 100%
    assert r2.meta_esperada == 200.0
    assert r2.cumplimiento_pct == 100.0
    assert r2.avance_pct == pytest.approx(28.6, abs=0.05)
    assert r2.estado == "cumple"

    # Upsert: mismo lunes, se sobreescribe (no se duplica la fila)
    r3 = guardar_valor(m.id, lunes - timedelta(weeks=1), ValorSemanalIn(valor=50),
                       db=db, current=usuario)
    assert r3.consolidado == 150.0
    assert r3.semanas_con_dato == 2
    assert db.query(RetoValorSemanal).filter(RetoValorSemanal.metrica_id == m.id).count() == 2
    assert r3.estado == "atencion"  # 150/200 = 75%

    # Borrar el dato de una semana = mandar valor null
    r4 = guardar_valor(m.id, lunes - timedelta(weeks=1), ValorSemanalIn(valor=None),
                       db=db, current=usuario)
    assert r4.consolidado == 100.0
    assert r4.semanas_con_dato == 1


def test_valor_rechaza_semana_que_no_es_lunes(db):
    reto = _reto_alrededor_de_hoy(db)
    m = crear_metrica(reto.id, MetricaCreate(nombre="MWh"), db=db, current=None)

    with pytest.raises(HTTPException) as exc:
        guardar_valor(m.id, _lunes_hoy() + timedelta(days=1), ValorSemanalIn(valor=1),
                      db=db, current=None)
    assert exc.value.status_code == 400
    assert exc.value.detail == "La semana debe empezar en lunes"
    assert db.query(RetoValorSemanal).count() == 0


def test_valor_rechaza_semana_fuera_del_rango(db):
    reto = _reto_alrededor_de_hoy(db)
    m = crear_metrica(reto.id, MetricaCreate(nombre="MWh"), db=db, current=None)

    for lunes_fuera in (_lunes_hoy() - timedelta(weeks=10), _lunes_hoy() + timedelta(weeks=10)):
        with pytest.raises(HTTPException) as exc:
            guardar_valor(m.id, lunes_fuera, ValorSemanalIn(valor=1), db=db, current=None)
        assert exc.value.status_code == 400
        assert exc.value.detail == "La semana está fuera del rango del trimestre"
    assert db.query(RetoValorSemanal).count() == 0


def test_valor_metrica_inexistente(db):
    with pytest.raises(HTTPException) as exc:
        guardar_valor(999, _lunes_hoy(), ValorSemanalIn(valor=1), db=db, current=None)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# GET /retos/{id}
# ---------------------------------------------------------------------------

def test_detalle_trae_semanas_y_valores_indexados(db):
    reto = _reto_alrededor_de_hoy(db)
    usuario = _usuario(db)
    m = crear_metrica(reto.id, MetricaCreate(nombre="MWh", meta=700), db=db, current=None)
    lunes = _lunes_hoy()
    guardar_valor(m.id, lunes, ValorSemanalIn(valor=90, nota="arranque lento"),
                  db=db, current=usuario)

    detalle = obtener_reto(reto.id, db=db, _=None)

    assert detalle.total_semanas == 7
    assert detalle.semana_actual == 3
    assert detalle.estado_periodo == "en_curso"
    assert len(detalle.semanas) == 7
    assert detalle.semanas[0].etiqueta == "S1"
    assert detalle.semanas[2].es_actual is True
    assert detalle.semanas[3].es_futura is True
    # El rango arranca en lunes: ninguna semana es parcial
    assert not any(s.parcial for s in detalle.semanas)

    celda = detalle.valores[str(m.id)][lunes.isoformat()]
    assert celda.valor == 90.0
    assert celda.nota == "arranque lento"
    assert celda.actualizado_por == "Juan José"
    assert detalle.semanas_con_datos == 1


def test_detalle_no_muestra_valores_fuera_del_rango_pero_no_los_borra(db):
    reto = _reto_alrededor_de_hoy(db)
    m = crear_metrica(reto.id, MetricaCreate(nombre="MWh"), db=db, current=None)
    lunes_viejo = _lunes_hoy() - timedelta(weeks=2)
    guardar_valor(m.id, lunes_viejo, ValorSemanalIn(valor=10), db=db, current=None)

    # Se recorta el rango dejando el valor por fuera
    actualizar_reto(reto.id, RetoUpdate(fecha_inicio=_lunes_hoy()), db=db, current=None)
    detalle = obtener_reto(reto.id, db=db, _=None)

    assert detalle.valores == {str(m.id): {}}
    assert detalle.metricas[0].consolidado is None
    # el registro sigue en la base
    assert db.query(RetoValorSemanal).filter(
        RetoValorSemanal.semana_inicio == lunes_viejo).count() == 1


def test_detalle_404(db):
    with pytest.raises(HTTPException) as exc:
        obtener_reto(999, db=db, _=None)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /retos/{id}
# ---------------------------------------------------------------------------

def test_patch_reto_valida_el_rango(db):
    reto = _reto_alrededor_de_hoy(db)

    with pytest.raises(HTTPException) as exc:
        actualizar_reto(reto.id, RetoUpdate(fecha_fin=reto.fecha_inicio), db=db, current=None)
    assert exc.value.status_code == 400
    assert exc.value.detail == "La fecha de fin debe ser posterior a la de inicio"

    with pytest.raises(HTTPException) as exc:
        actualizar_reto(reto.id, RetoUpdate(fecha_fin=reto.fecha_inicio + timedelta(weeks=61)),
                        db=db, current=None)
    assert exc.value.status_code == 400
    assert exc.value.detail == "El rango no puede superar 60 semanas"


def test_patch_reto_actualiza_nombre_y_fechas(db):
    reto = _reto_alrededor_de_hoy(db)
    nuevo_fin = reto.fecha_inicio + timedelta(weeks=10, days=6)

    detalle = actualizar_reto(reto.id, RetoUpdate(nombre="Retos del equipo", descripcion="foco Q",
                                                  fecha_fin=nuevo_fin), db=db, current=None)
    assert detalle.nombre == "Retos del equipo"
    assert detalle.descripcion == "foco Q"
    assert detalle.fecha_fin == nuevo_fin
    assert detalle.total_semanas == 11


# ---------------------------------------------------------------------------
# copiar-desde
# ---------------------------------------------------------------------------

def test_copiar_desde_clona_activas_sin_valores(db):
    origen = _reto_alrededor_de_hoy(db, trimestre=1)
    destino = _reto_alrededor_de_hoy(db, trimestre=2)

    a = crear_metrica(origen.id, MetricaCreate(nombre="MWh comercializados", unidad="MWh",
                                               meta=1200, tipo_agregacion="promedio",
                                               direccion="menor_mejor", decimales=2,
                                               responsable="Laura"),
                      db=db, current=None)
    crear_metrica(origen.id, MetricaCreate(nombre="Fallas abiertas"), db=db, current=None)
    inactiva = crear_metrica(origen.id, MetricaCreate(nombre="Vieja"), db=db, current=None)
    actualizar_metrica(inactiva.id, MetricaUpdate(activa=False), db=db, current=None)
    guardar_valor(a.id, _lunes_hoy(), ValorSemanalIn(valor=42), db=db, current=None)

    copiadas = copiar_metricas(destino.id, origen.id, db=db, current=None)

    assert [m.nombre for m in copiadas] == ["MWh comercializados", "Fallas abiertas"]
    clon = copiadas[0]
    assert clon.reto_id == destino.id
    assert clon.unidad == "MWh"
    assert clon.meta == 1200.0
    assert clon.tipo_agregacion == "promedio"
    assert clon.direccion == "menor_mejor"
    assert clon.decimales == 2
    assert clon.responsable == "Laura"
    # sin valores
    assert clon.semanas_con_dato == 0
    assert clon.consolidado is None
    assert db.query(RetoValorSemanal).filter(RetoValorSemanal.metrica_id == clon.id).count() == 0


def test_copiar_desde_no_duplica_por_nombre(db):
    origen = _reto_alrededor_de_hoy(db, trimestre=1)
    destino = _reto_alrededor_de_hoy(db, trimestre=2)
    crear_metrica(origen.id, MetricaCreate(nombre="MWh"), db=db, current=None)
    crear_metrica(origen.id, MetricaCreate(nombre="Fallas"), db=db, current=None)
    crear_metrica(destino.id, MetricaCreate(nombre="mwh"), db=db, current=None)

    copiadas = copiar_metricas(destino.id, origen.id, db=db, current=None)

    assert [m.nombre for m in copiadas] == ["Fallas"]
    assert db.query(RetoMetrica).filter(RetoMetrica.reto_id == destino.id).count() == 2


def test_copiar_desde_valida_ids(db):
    origen = _reto_alrededor_de_hoy(db, trimestre=1)
    destino = _reto_alrededor_de_hoy(db, trimestre=2)

    with pytest.raises(HTTPException) as exc:
        copiar_metricas(destino.id, destino.id, db=db, current=None)
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        copiar_metricas(999, origen.id, db=db, current=None)
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        copiar_metricas(destino.id, 999, db=db, current=None)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Agregados del Q
# ---------------------------------------------------------------------------

def test_avance_global_promedia_las_metricas_con_dato(db):
    reto = _reto_alrededor_de_hoy(db)
    lunes = _lunes_hoy()
    # El Q va en la S3 de 7, con 2 semanas ya cerradas → esperada = meta * 2/7 = 200
    m1 = crear_metrica(reto.id, MetricaCreate(nombre="A", meta=700), db=db, current=None)
    m2 = crear_metrica(reto.id, MetricaCreate(nombre="B", meta=700), db=db, current=None)
    crear_metrica(reto.id, MetricaCreate(nombre="C sin dato", meta=700), db=db, current=None)

    guardar_valor(m1.id, lunes, ValorSemanalIn(valor=200), db=db, current=None)   # 100%
    guardar_valor(m2.id, lunes, ValorSemanalIn(valor=100), db=db, current=None)   # 50%

    detalle = obtener_reto(reto.id, db=db, _=None)
    assert detalle.total_metricas == 3
    assert detalle.avance_global_pct == 75.0
    assert detalle.semanas_con_datos == 1
    assert [m.estado for m in detalle.metricas] == ["cumple", "en_riesgo", "sin_datos"]
