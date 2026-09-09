"""Editar un contrato de energía ya creado, y crear uno PLC con sus 24 horas.

La API externa sí permite editar —`OPTIONS` sobre el detalle responde
`GET, PUT, PATCH`— tanto del contrato como del vínculo con el proyecto y de los
pisos y techos. Lo que faltaba era exponerlo: la plataforma solo sabía crear.

**No hay `DELETE` en ninguno de los tres**, así que se puede editar y agregar,
pero no desvincular un proyecto ni borrar un contrato. La vista tiene que
reflejar eso en vez de prometerlo.

De paso se arregla un bug del alta: `floor` y `roof` estaban declarados como un
`FloatField` —un número suelto— pero son las **24 horas del día**, que es lo que
manda el formulario (`parseHoras`) y lo que pide la API (§3.5). Con eso, crear
un contrato PLC con sus pisos y techos respondía 400 y era imposible.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

HORAS = [0.0] * 6 + [
    31.0, 165.0, 341.0, 453.0, 517.0, 560.0, 570.0, 555.0, 515.0, 430.0,
    320.0, 184.0, 55.0,
] + [0.0] * 5


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


# ── Alta: las 24 horas ───────────────────────────────────────────────────────

def _alta(**proyecto):
    from api.v1.liquidaciones_proxy.serializers import ContratoEnergiaSerializer

    return ContratoEnergiaSerializer(data={
        "date_from": "2026-01-01", "date_to": "2026-12-31",
        "contract_type": "ppa_pay_as_contracted", "tariff_price_type": "ppa",
        "code": "C1", "company": 61,
        "proyectos": [{"project": "granja", "energy_price": 3, **proyecto}],
    })


def test_un_plc_se_crea_con_sus_24_horas():
    """El caso que devolvía 400: floor y roof son la curva del día, no un número."""
    s = _alta(floor=HORAS, roof=HORAS)
    assert s.is_valid(), s.errors
    assert s.validated_data["proyectos"][0]["floor"] == HORAS


def test_una_curva_que_no_trae_24_horas_se_rechaza():
    """23 valores dan una liquidación silenciosamente mal: mejor un 400."""
    assert not _alta(floor=HORAS[:23]).is_valid()
    assert not _alta(floor=HORAS + [1.0]).is_valid()


# ── Edición ──────────────────────────────────────────────────────────────────

def _edicion(datos):
    from api.v1.liquidaciones_proxy.serializers import (
        ContratoEnergiaUpdateSerializer,
    )

    return ContratoEnergiaUpdateSerializer(data=datos)


def test_editar_admite_cambiar_un_solo_campo():
    """Es un PATCH: lo que no se manda, no se toca."""
    s = _edicion({"code": "90060"})
    assert s.is_valid(), s.errors
    assert s.validated_data == {"code": "90060"}


def test_un_proyecto_con_id_es_un_vinculo_que_ya_existe():
    s = _edicion({"proyectos": [{"id": 12, "energy_price": 3}]})
    assert s.is_valid(), s.errors
    assert s.validated_data["proyectos"][0]["id"] == 12


def test_un_proyecto_sin_id_es_uno_nuevo_y_exige_el_topico():
    assert _edicion({"proyectos": [{"project": "granja"}]}).is_valid()
    # Sin `id` ni `project` no se sabe a qué se refiere.
    assert not _edicion({"proyectos": [{"energy_price": 3}]}).is_valid()


def test_los_pisos_y_techos_se_editan_por_su_id():
    s = _edicion({"proyectos": [{"id": 12, "piso_id": 5, "floor": HORAS}]})
    assert s.is_valid(), s.errors
    assert s.validated_data["proyectos"][0]["piso_id"] == 5


# ── El listado tiene que traer con qué editar ────────────────────────────────

def test_el_listado_expone_el_topico_y_las_curvas():
    """Sin el id de cada curva no hay a quién hacerle PATCH."""
    from apps.liquidaciones.services.agregados import build_contratos_energia

    filas = build_contratos_energia(
        contratos=[{"id": 1, "date_from": "2026-01-01", "contract_type": "ppa_pay_as_contracted"}],
        vinculos=[{"id": 12, "contract_energy": 1, "project": "granja", "energy_price": 3}],
        cantidades=[{"id": 5, "contract_energy_project": 12, "concept_type": "floor", "hours": HORAS}],
        catalogos={"empresas": [], "precios_energia": [{"id": 3, "name": "P1"}]},
        nombres={"granja": "Granja Solar Uno"},
    )
    proyecto = filas[0]["proyectos"][0]
    assert proyecto["topico"] == "granja", "hace falta para precargar el formulario"
    assert proyecto["proyecto"] == "Granja Solar Uno"
    assert proyecto["piso"] == {"id": 5, "hours": HORAS}
    assert proyecto["techo"] is None
    # Lo que ya se mostraba no cambia.
    assert proyecto["tiene_piso"] is True and proyecto["tiene_techo"] is False
