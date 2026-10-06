"""A qué clientes ofrece la pantalla del Reporte CGM enviarles el reporte.

Caso real, 2026-10-02: proyectos con inversionistas salían "Sin inversionistas
— corregir". Al portar `clientes_cgm` de FastAPI (2026-09-04) la lista se armó
solo con el puntero de área CGM; el respaldo de FastAPI --los inversionistas
vigentes cuando no hay puntero-- se perdió, y con él esos destinatarios.

El envío sí los tenía en cuenta (`contactos.proyecto_ids_por_cliente`): el
error era solo de la lista. Ahora la lista y los correos leen la misma regla,
`contactos.clientes`.

Sin base de datos (el repo no tiene `pytest-django`): las dos tablas se
reemplazan por dobles que devuelven las filas ya filtradas.
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


class _Qs:
    """Lo justo de un QuerySet: `filter` no filtra (las filas ya vienen
    filtradas), `values_list` devuelve las tuplas y `first` la primera."""

    def __init__(self, filas):
        self.filas = filas

    def filter(self, *a, **kw):
        return self

    def values_list(self, *a, **kw):
        return self

    def first(self):
        return self.filas[0] if self.filas else None

    def __iter__(self):
        return iter(self.filas)


@pytest.fixture
def tablas(monkeypatch):
    from types import SimpleNamespace

    from apps.clientes.services import contactos

    datos = {"punteros": [], "inversionistas": []}
    monkeypatch.setattr(contactos, "cl_models", SimpleNamespace(
        ProyectoAreaContacto=SimpleNamespace(
            objects=SimpleNamespace(filter=lambda **kw: _Qs(datos["punteros"]))),
    ))
    monkeypatch.setattr(contactos, "py_models", SimpleNamespace(
        ProyectoInversionista=SimpleNamespace(
            objects=SimpleNamespace(filter=lambda **kw: _Qs(datos["inversionistas"]))),
    ))
    return datos


def test_sin_puntero_salen_los_inversionistas_vigentes(tablas):
    """El bug: este proyecto salía "Sin inversionistas"."""
    from apps.clientes.services import contactos

    tablas["inversionistas"] = [(7, "INVERSIONES SOL"), (9, "FONDO VERDE")]

    assert contactos.clientes("cgm", 1) == [
        {"id": 7, "nombre": "INVERSIONES SOL"},
        {"id": 9, "nombre": "FONDO VERDE"},
    ]


def test_con_puntero_sale_solo_el_cliente_del_puntero(tablas):
    from apps.clientes.services import contactos

    tablas["punteros"] = [(3, "CONTACTO CGM")]
    tablas["inversionistas"] = [(7, "INVERSIONES SOL")]

    assert contactos.clientes("cgm", 1) == [{"id": 3, "nombre": "CONTACTO CGM"}]


def test_un_inversionista_con_dos_participaciones_sale_una_vez(tablas):
    from apps.clientes.services import contactos

    tablas["inversionistas"] = [(7, "INVERSIONES SOL"), (7, "INVERSIONES SOL")]

    assert contactos.clientes("cgm", 1) == [{"id": 7, "nombre": "INVERSIONES SOL"}]


def test_la_lista_de_la_pantalla_usa_la_misma_regla(tablas, monkeypatch):
    from api.v1.fronteras import queryset
    from apps.clientes.services import contactos

    tablas["inversionistas"] = [(7, "INVERSIONES SOL")]
    monkeypatch.setattr(contactos, "correos", lambda tipo, **kw: [f"cgm{kw['cliente_id']}@x.co"])

    assert queryset.clientes_cgm([1, 1, None]) == {
        1: [{"id": 7, "nombre": "INVERSIONES SOL", "correos": ["cgm7@x.co"]}],
    }
