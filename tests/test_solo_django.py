"""El arbol Django (`apps/`, `api/`, `config/`) no importa FastAPI ni SQLAlchemy.

Django reemplazo a FastAPI el 2026-09-04, pero el paquete `app/` sigue en el
repo --266 archivos-- porque `apps/` le importa clientes puros (MGS, SMTP, los
parsers de correo de mandatos, `liquidaciones_loader`). Parece vivo, y ahi esta
el riesgo: quien entre a iterar puede concluir que ese es el arbol donde se
escribe, o traerse un `Depends`/`Session` a un modulo nuevo bajo `apps/`.

La regla esta escrita en `CLAUDE.md`, pero la prosa se ignora y un test rojo no.
Hoy pasa en verde: no hay ni un import de `fastapi` ni de `sqlalchemy` en los
534 archivos de los tres paquetes. Este archivo no arregla nada, vigila que siga
asi.

Se mira el AST y no el texto: hay docstrings que NOMBRAN a las dos librerias a
proposito (`apps/plataforma/models.py` explica que su tabla no tiene modelo
SQLAlchemy), y un grep las contaria como violaciones.

La escotilla que existe --`apps/liquidaciones/services/excel.py` importa
`app.core.database.SessionLocal`-- no la ve este test, y es correcto: importa
`app.*`, no `sqlalchemy`. Los imports de `app.*` son la deuda que se paga
portando esos 21 modulos; los de `sqlalchemy` serian deuda NUEVA.
"""
import ast
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PAQUETES = ["apps", "api", "config"]
PROHIBIDOS = ("fastapi", "sqlalchemy")


def _raiz_del_modulo(nombre: str) -> str:
    return nombre.split(".")[0].lower()


def _imports_prohibidos(archivo: Path) -> list[str]:
    arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
    encontrados = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres = [alias.name for alias in nodo.names]
        elif isinstance(nodo, ast.ImportFrom):
            # `from . import x` deja module en None; el nivel relativo nunca
            # puede apuntar a una libreria externa.
            nombres = [nodo.module] if nodo.module and not nodo.level else []
        else:
            continue
        for nombre in nombres:
            if _raiz_del_modulo(nombre) in PROHIBIDOS:
                encontrados.append(f"{archivo.relative_to(RAIZ)}:{nodo.lineno} → {nombre}")
    return encontrados


def test_arbol_django_no_importa_fastapi_ni_sqlalchemy():
    violaciones = [
        v
        for paquete in PAQUETES
        for archivo in sorted((RAIZ / paquete).rglob("*.py"))
        for v in _imports_prohibidos(archivo)
    ]
    assert not violaciones, (
        "El arbol Django importa FastAPI o SQLAlchemy. Codigo nuevo va a "
        "`apps/<dominio>/` + `api/v1/<recurso>/` con el ORM de Django "
        "(ver CLAUDE.md):\n  " + "\n  ".join(violaciones)
    )
