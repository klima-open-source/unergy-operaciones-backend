"""Ningun modulo del arbol Django puede pedirle a otro un atributo que no tiene.

Bug real (2026-09-07), del log de produccion:

    Operaciones | Facturacion | list fallo:
      module 'apps.mercado_xm.models' has no attribute 'IppMensual'
      apps/facturacion/services/calculo.py:163

`IppMensual` vive en `apps.ppa.models`, no en `mercado_xm` --y `ppa_models` ya
estaba importado en ese mismo archivo. Dos endpoints caidos por lo mismo:
`GET /api/v1/facturacion` y `DELETE /api/v1/liquidaciones/{id}/limpiar`
(`lq_models.LiquidacionMandato`, que vive en `apps.mandatos.models`).

**Por que no lo vio `test_nombres_definidos.py`**: ese corre `ruff --select
F821`, que detecta *nombres* indefinidos. `mx_models.IppMensual` es un nombre
perfectamente definido con un *atributo* que no existe: F821 no puede verlo y el
modulo importa sin error. Es la cuarta cara de la misma clase de bug del port
--un nombre que no existe-- despues del `select_related("operador")` de fronteras,
el `duration_hours` de mantenimiento-impacto y las constantes del Reporte de
Energia; y se le escapa por construccion.

El barrido es estatico: `ast` para mapear los alias de import de cada archivo,
`importlib` para resolver el modulo destino. Sin base de datos y sin red.
"""
import ast
import importlib
import os
from pathlib import Path

import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")

RAIZ = Path(__file__).resolve().parent.parent
PAQUETES = ["apps", "api", "config"]

# Modulo -> cuantos atributos inexistentes tiene hoy. Igual que el BASELINE de
# test_nombres_definidos.py: existe por si algun dia hace falta declarar deuda a
# proposito, pero cada entrada es un AttributeError en produccion esperando a que
# alguien abra esa vista, asi que lo normal es que siga vacio.
BASELINE: dict[str, int] = {
    # VACIO. Los 3 que existian el 2026-09-07 --el `IppMensual` de facturacion y
    # los dos de mandatos en liquidaciones-- se arreglaron ese mismo dia.
}

_SEPARADOR = "\n  "


@pytest.fixture(scope="module", autouse=True)
def _django_listo():  # noqa: PT004
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _alias_de_modulo(arbol: ast.Module) -> dict[str, str]:
    """`{alias: modulo}` de los imports que traen un MODULO, no un objeto.

    `import a.b as c` -> c = a.b; `from a.b import c as d` -> d = a.b.c, pero
    solo si `a.b.c` es importable como modulo (si `c` es una clase, el import
    falla y el alias se descarta: pedirle atributos a una clase no es este bug).
    """
    alias: dict[str, str] = {}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            for nom in nodo.names:
                if nom.asname:
                    alias[nom.asname] = nom.name
        elif isinstance(nodo, ast.ImportFrom):
            if nodo.level or not nodo.module:
                continue  # relativo: no se puede resolver sin el paquete
            for nom in nodo.names:
                if nom.name == "*":
                    continue
                alias[nom.asname or nom.name] = f"{nodo.module}.{nom.name}"
    return alias


def _nombres_reasignados(arbol: ast.Module) -> set[str]:
    """Nombres que el modulo vuelve a asignar (o usa como parametro/loop).

    Un alias reasignado deja de apuntar al modulo importado, asi que sus
    atributos no se pueden verificar estaticamente.
    """
    sombras: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Name) and isinstance(nodo.ctx, (ast.Store, ast.Del)):
            sombras.add(nodo.id)
        elif isinstance(nodo, ast.arg):
            sombras.add(nodo.arg)
        elif isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            sombras.add(nodo.name)
        elif isinstance(nodo, ast.ExceptHandler) and nodo.name:
            sombras.add(nodo.name)
    return sombras


def _modulo(ruta: str):
    """El modulo importado, o `None` si no se puede resolver como modulo."""
    try:
        return importlib.import_module(ruta)
    except Exception:  # noqa: BLE001 -- no es un modulo, o el entorno no lo tiene
        return None


def _archivos():
    for paquete in PAQUETES:
        yield from sorted((RAIZ / paquete).rglob("*.py"))


def _hallazgos() -> list[str]:
    """`ruta:linea alias.Atributo` de cada atributo que el destino no tiene."""
    cache: dict[str, object] = {}
    encontrados = []

    for archivo in _archivos():
        try:
            arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        alias = _alias_de_modulo(arbol)
        if not alias:
            continue
        vigentes = {a: m for a, m in alias.items() if a not in _nombres_reasignados(arbol)}
        if not vigentes:
            continue

        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Attribute) or not isinstance(nodo.value, ast.Name):
                continue
            ruta_mod = vigentes.get(nodo.value.id)
            if ruta_mod is None:
                continue
            if ruta_mod not in cache:
                cache[ruta_mod] = _modulo(ruta_mod)
            modulo = cache[ruta_mod]
            if modulo is None:
                continue
            if not hasattr(modulo, nodo.attr):
                relativa = archivo.relative_to(RAIZ).as_posix()
                encontrados.append(
                    f"{relativa}:{nodo.lineno} {nodo.value.id}.{nodo.attr} "
                    f"-- {ruta_mod} no lo tiene"
                )
    return encontrados


@pytest.fixture(scope="module")
def hallazgos(_django_listo) -> list[str]:
    return _hallazgos()


def _por_modulo(hallazgos: list[str]) -> dict[str, int]:
    from collections import Counter

    return Counter(h.split(":")[0] for h in hallazgos)


def test_hay_archivos_que_revisar():
    """Sanity: si el recorrido deja de encontrarlos, el test pasaria vacio."""
    assert len(list(_archivos())) >= 200


def test_ningun_modulo_nuevo_pide_un_atributo_inexistente(hallazgos):
    conteo = _por_modulo(hallazgos)
    nuevos = sorted(set(conteo) - set(BASELINE))
    detalle = [h for h in hallazgos if h.split(":")[0] in nuevos]
    assert not detalle, (
        "Estos modulos le piden a otro un atributo que no tiene. Cada uno "
        "revienta con AttributeError en cuanto se ejecute esa linea, y "
        "`ruff --select F821` no puede verlo:"
        + _SEPARADOR + _SEPARADOR.join(detalle)
    )


def test_ningun_modulo_conocido_empeora(hallazgos):
    conteo = _por_modulo(hallazgos)
    peores = [
        f"{m}: {conteo[m]} ahora, {BASELINE[m]} en el baseline"
        for m in sorted(BASELINE) if conteo.get(m, 0) > BASELINE[m]
    ]
    assert not peores, (
        "Se agregaron atributos inexistentes:" + _SEPARADOR + _SEPARADOR.join(peores)
    )


def test_el_baseline_no_declara_deuda_que_ya_no_existe(hallazgos):
    conteo = _por_modulo(hallazgos)
    resueltos = [
        f"{m}: {conteo.get(m, 0)} ahora, {BASELINE[m]} declarados"
        for m in sorted(BASELINE) if conteo.get(m, 0) < BASELINE[m]
    ]
    assert not resueltos, (
        "Ya se arreglaron atributos que el baseline sigue declarando. Bajar el "
        "numero (o borrar la linea):" + _SEPARADOR + _SEPARADOR.join(resueltos)
    )
