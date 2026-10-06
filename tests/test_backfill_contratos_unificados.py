"""El comando de copia a la tabla única (plan 08): lo que se puede probar sin Postgres.

La copia completa se ensaya contra la copia local de producción (`pg17`), paso 5 del
plan: usa `information_schema`, `setval` y `row_number()`.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def test_copia_por_nombre_las_columnas_que_existen_en_los_dos_lados():
    from apps.contratos.management.commands.backfill_contratos_unificados import plan_de_columnas

    assert plan_de_columnas({"id", "a", "b"}, {"id", "a", "b", "c"}, set()) == ["a", "b"]


def test_una_columna_vieja_sin_destino_detiene_la_copia():
    """Que no se pierda un dato en silencio."""
    from django.core.management.base import CommandError

    from apps.contratos.management.commands.backfill_contratos_unificados import plan_de_columnas

    with pytest.raises(CommandError, match="nueva_sin_destino"):
        plan_de_columnas({"id", "a", "nueva_sin_destino"}, {"id", "a"}, set())


def test_las_columnas_muertas_declaradas_no_detienen_la_copia():
    from apps.contratos.management.commands.backfill_contratos_unificados import (
        COLUMNAS_MUERTAS, plan_de_columnas,
    )

    muertas = COLUMNAS_MUERTAS["ppa_contratos"]
    assert plan_de_columnas({"id", "a"} | muertas, {"id", "a"}, muertas) == ["a"]


def test_el_grupo_de_un_contrato_de_servicio_sale_del_catalogo():
    """Una sola lista: la de `grupos`. Nada de PPA en el CASE de servicio."""
    from apps.contratos.management.commands.backfill_contratos_unificados import (
        _grupo_de_servicio_sql,
    )
    from apps.contratos.services import grupos

    sql = _grupo_de_servicio_sql()
    for sub in grupos.SUBSERVICIOS_DE_CONTRATO_SERVICIO:
        assert f"WHEN '{sub}' THEN '{grupos.GRUPO_DE_SUBSERVICIO[sub]}'" in sql
    assert grupos.COMPRA not in sql and grupos.VENTA not in sql
