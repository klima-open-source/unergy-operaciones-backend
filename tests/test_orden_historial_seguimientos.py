"""El historial de una falla sale ordenado desde la base, no desde cada vista.

Bug real (2026-09-07): `FallaSeguimiento` no declaraba `ordering`, asi que el
orden de `GET /fallas/{id}` -> `seguimientos` lo decidia Postgres. Sin `ORDER
BY` el orden **no esta definido**: suele coincidir con el de insercion, pero
cambia tras updates o un vacuum.

Cada pantalla lo resolvia por su cuenta, y no igual:

  · FallaDetailView.vue    -> ordenaba en el cliente (`sortedSeguimientos`)
  · FallaDetailSheet.vue   -> renderizaba tal cual llegaba (movil)

O sea que la misma falla podia mostrar su cronologia bien en el escritorio y
desordenada en el telefono. Es la tercera vez que esas dos vistas divergen por
una decision duplicada; las otras dos fueron `gaia_snapshot_*` y el numero de
Generacion Solar (ver app/features/solar/serieSolar.js en el frontend).

Ahora el orden vive en el modelo y el frontend solo renderiza. Este test lo
sostiene: si alguien quita el `ordering`, el escritorio y el movil vuelven a
depender del azar y nada mas lo detecta -- una cronologia desordenada se ve como
un dato raro, no como un error.

No hace falta base de datos: `str(qs.query)` compila el SQL y ahi se ve el
ORDER BY.
"""
import pytest

pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def test_el_modelo_declara_el_orden():
    from apps.monitoreo.models import FallaSeguimiento

    assert FallaSeguimiento._meta.ordering == ["-created_at", "-id"], (
        "El historial tiene que salir del mas reciente al mas viejo desde la "
        "base. `-id` desempata dos notas del mismo segundo."
    )


def test_el_sql_lleva_el_order_by():
    """Que `Meta.ordering` exista no basta: se comprueba que llegue al SQL."""
    from apps.monitoreo.models import FallaSeguimiento

    sql = str(FallaSeguimiento.objects.filter(falla_id=1).query)

    assert "ORDER BY" in sql, sql
    assert '"created_at" DESC' in sql, sql
    assert '"id" DESC' in sql, sql


def test_el_serializer_anidado_hereda_el_orden():
    """`FallaDetalleSerializer.seguimientos` sale del related_name, asi que usa
    el manager por defecto -- y con el, el ordering del modelo. Se fija aca
    porque es el camino real por el que el frontend lee el historial."""
    from apps.monitoreo.models import Falla

    campo = Falla._meta.get_field("seguimientos")
    modelo_hijo = campo.related_model

    assert campo.remote_field.name == "falla"
    assert modelo_hijo._meta.ordering == ["-created_at", "-id"]
