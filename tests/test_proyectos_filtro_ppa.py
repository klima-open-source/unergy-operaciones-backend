"""Filtro por contrato PPA especifico en GET /proyectos (ver docs/API_PROYECTOS.md).

Antes de esto, "PPA" solo se podia filtrar como bandera de servicio contratado
(`servicio=ppa`, columna booleana `srv_ppa`), no como vinculo a un contrato PPA
real (tabla `ppa_contratos`, vinculada via `ppa_contrato_proyectos`). Este filtro
nuevo (`ppa_id`, repetible, y `sin_ppa`) sigue el mismo patron de join que ya usa
`app/api/v1/ppa.py::list_contratos` para el filtro inverso (`proyecto_id`).
"""
import pytest
from sqlalchemy import create_engine, BigInteger
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from app.models.base import Base
import app.models  # noqa: F401
from app.models import Proyecto, PPAContrato
from app.models.proyectos import (
    ProyectoInversionista, ProyectoInfoTecnica, ProyectoInversor,
)
from app.models.contactos import ProyectoAreaContacto, Contacto
from app.models.clientes import Cliente
from app.models.fronteras import Frontera
from app.models.operadores_red import OperadorRed
from app.api.v1 import proyectos as proyectos_api


@compiles(JSONB, "sqlite")
def _jsonb_as_text(element, compiler, **kw):
    return "TEXT"


@compiles(BigInteger, "sqlite")
def _bigint_as_integer(element, compiler, **kw):
    return "INTEGER"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Proyecto.__table__, Cliente.__table__, ProyectoInversionista.__table__,
            ProyectoInfoTecnica.__table__,
            ProyectoInversor.__table__, ProyectoAreaContacto.__table__, Contacto.__table__,
            Frontera.__table__, OperadorRed.__table__,
            PPAContrato.__table__, Base.metadata.tables["ppa_contrato_proyectos"],
            Base.metadata.tables["oportunidad_oferta_proyectos"],
        ],
    )
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


_ids = iter(range(1, 10_000))


def _proyecto(db, **kw):
    p = Proyecto(id=next(_ids), nombre_comercial=kw.pop("nombre_comercial", "Proyecto"), **kw)
    db.add(p)
    db.commit()
    return p


def _contrato(db, **kw):
    c = PPAContrato(id=next(_ids), **kw)
    db.add(c)
    db.commit()
    return c


def _vincular(db, proyecto, contrato):
    proyecto.ppa_contratos  # noqa: B018 -- fuerza el mapeo antes de usar la tabla puente
    db.execute(
        Base.metadata.tables["ppa_contrato_proyectos"].insert().values(
            contrato_id=contrato.id, proyecto_id=proyecto.id,
        )
    )
    db.commit()


def _listar(db, **kw):
    kw.setdefault("page", 1)
    kw.setdefault("size", 20)
    kw.setdefault("ppa_id", None)
    kw.setdefault("sin_ppa", None)
    return proyectos_api.list_proyectos(db=db, _=None, **kw)


def test_filtra_por_un_contrato_ppa_especifico(db):
    con_ppa = _proyecto(db, nombre_comercial="Con PPA")
    sin_ppa = _proyecto(db, nombre_comercial="Sin PPA")
    contrato = _contrato(db, nombre_interno="Contrato A")
    _vincular(db, con_ppa, contrato)

    out = _listar(db, ppa_id=[contrato.id])

    assert out["total"] == 1
    assert [p.id for p in out["items"]] == [con_ppa.id]


def test_filtra_por_varios_contratos_ppa(db):
    p1 = _proyecto(db, nombre_comercial="Uno")
    p2 = _proyecto(db, nombre_comercial="Dos")
    p3 = _proyecto(db, nombre_comercial="Tres")
    c1 = _contrato(db, nombre_interno="Contrato 1")
    c2 = _contrato(db, nombre_interno="Contrato 2")
    _vincular(db, p1, c1)
    _vincular(db, p2, c2)

    out = _listar(db, ppa_id=[c1.id, c2.id])

    assert {p.id for p in out["items"]} == {p1.id, p2.id}
    assert p3.id not in {p.id for p in out["items"]}


def test_filtra_proyectos_sin_ningun_ppa(db):
    con_ppa = _proyecto(db, nombre_comercial="Con PPA")
    sin_ppa = _proyecto(db, nombre_comercial="Sin PPA")
    contrato = _contrato(db, nombre_interno="Contrato A")
    _vincular(db, con_ppa, contrato)

    out = _listar(db, sin_ppa=True)

    assert out["total"] == 1
    assert [p.id for p in out["items"]] == [sin_ppa.id]


def test_combina_contrato_especifico_con_sin_ppa_como_or(db):
    con_ppa_seleccionado = _proyecto(db, nombre_comercial="Seleccionado")
    con_otro_ppa = _proyecto(db, nombre_comercial="Otro PPA")
    sin_ppa = _proyecto(db, nombre_comercial="Sin PPA")
    c1 = _contrato(db, nombre_interno="Contrato 1")
    c2 = _contrato(db, nombre_interno="Contrato 2")
    _vincular(db, con_ppa_seleccionado, c1)
    _vincular(db, con_otro_ppa, c2)

    out = _listar(db, ppa_id=[c1.id], sin_ppa=True)

    assert {p.id for p in out["items"]} == {con_ppa_seleccionado.id, sin_ppa.id}


def test_un_proyecto_con_dos_contratos_seleccionados_no_se_duplica(db):
    p = _proyecto(db, nombre_comercial="Doble PPA")
    c1 = _contrato(db, nombre_interno="Contrato 1")
    c2 = _contrato(db, nombre_interno="Contrato 2")
    _vincular(db, p, c1)
    _vincular(db, p, c2)

    out = _listar(db, ppa_id=[c1.id, c2.id])

    assert out["total"] == 1
    assert [item.id for item in out["items"]] == [p.id]


def test_sin_filtro_de_ppa_trae_todos_como_antes(db):
    _proyecto(db, nombre_comercial="Con PPA")
    _proyecto(db, nombre_comercial="Sin PPA")

    out = _listar(db)

    assert out["total"] == 2


# ── Enrutamiento HTTP real ────────────────────────────────────────────────────
# Los tests de arriba llaman list_proyectos() directo, así que no cubren si
# FastAPI de verdad parsea "?ppa_id=1&ppa_id=2" como list[int] (Query(None) con
# ese tipo requiere la sintaxis repetida, no "ppa_id[]=1&ppa_id[]=2").

@pytest.fixture
def client(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.core.database import get_db

    app = FastAPI()
    app.include_router(proyectos_api.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_ruta_http_parsea_ppa_id_repetido_como_lista(db, client):
    incluido = _proyecto(db, nombre_comercial="Incluido")
    excluido = _proyecto(db, nombre_comercial="Excluido")
    c1 = _contrato(db, nombre_interno="Contrato 1")
    c2 = _contrato(db, nombre_interno="Contrato 2")
    _vincular(db, incluido, c1)
    _vincular(db, excluido, c2)

    r = client.get("/api/v1/proyectos", params={"ppa_id": [c1.id]})

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["total"] == 1
    assert cuerpo["items"][0]["id"] == incluido.id


def test_ruta_http_sin_ppa(db, client):
    con_ppa = _proyecto(db, nombre_comercial="Con PPA")
    sin_ppa = _proyecto(db, nombre_comercial="Sin PPA")
    _vincular(db, con_ppa, _contrato(db, nombre_interno="Contrato 1"))

    r = client.get("/api/v1/proyectos", params={"sin_ppa": True})

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["total"] == 1
    assert cuerpo["items"][0]["id"] == sin_ppa.id
