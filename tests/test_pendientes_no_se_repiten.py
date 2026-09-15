"""Una sugerencia de "actualizar" que no cambia nada no se propone.

Bug reportado el 2026-09-15: al darle "Actualizar" a una sugerencia, esta
desaparecía de la lista y **volvía a aparecer al rato**. Con "Crear" no pasaba.

El mecanismo. `resolver_pendientes` propone actualizar cuando el candidato
empareja con un proyecto sólo POR NOMBRE, para que alguien confirme el vínculo.
Pero `_actualizar_desde_pendiente` **no pisa lo que ya tiene valor**: si el
proyecto ya tiene todos sus identificadores, confirmar no escribe nada, el
vínculo nunca queda registrado, y en la siguiente consulta vuelve a emparejar
por nombre y a proponerse. El botón no progresa nunca.

Con "Crear" no ocurre porque escribe una fila nueva con sus identificadores.

El caso real: la API de Unergy trae DOS entradas para la misma planta --los
tópicos `chima` y `chima_oriente`-- y el proyecto ya estaba vinculado a la
primera. La segunda emparejaba por nombre, no tenía nada que aportar, y volvía
siempre. Se destapó al sumar esa API como tercera fuente: antes ningún candidato
traía un `sub_project` alternativo.

Lo que se fija acá es que la sugerencia por nombre sobreviva **sólo si al
confirmarla se va a escribir algún identificador**. Con eso la lista siempre
avanza: o hay algo que registrar, o la sugerencia no está.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _hay(candidato, proyecto):
    from apps.proyectos.services.pendientes import _hay_vinculo_por_escribir

    return _hay_vinculo_por_escribir(candidato, proyecto)


def _candidato(**kw):
    from apps.proyectos.services.pendientes import _Candidato

    return _Candidato(**kw)


def _proyecto(**kw):
    """Un proyecto en memoria: la función sólo lee atributos."""
    from types import SimpleNamespace

    campos = {
        "origina_code": None, "codigo_tsf": None,
        "sunfactory_project_id": None, "sub_project": None,
    }
    campos.update(kw)
    return SimpleNamespace(**campos)


# ── El caso del bug ─────────────────────────────────────────────────────────


def test_el_proyecto_ya_vinculado_no_tiene_nada_que_escribir():
    """Chimá: el proyecto ya tiene `sub_project`, el candidato trae otro tópico
    de la misma planta. Confirmar no escribiría nada."""
    candidato = _candidato(sub_project="chima_oriente")
    proyecto = _proyecto(sub_project="chima", codigo_tsf="COLCORT7P1",
                         origina_code="COLCORT7P1_CHIMA_ORIENTE",
                         sunfactory_project_id=10)

    assert _hay(candidato, proyecto) is False


def test_un_identificador_vacio_si_es_algo_que_escribir():
    """El caso legítimo: el proyecto no tiene `sub_project` y el candidato lo
    trae. Confirmar deja el vínculo y la sugerencia no vuelve."""
    candidato = _candidato(sub_project="la_perdiz")
    proyecto = _proyecto(codigo_tsf="COLXXX1")

    assert _hay(candidato, proyecto) is True


@pytest.mark.parametrize("campo,valor", [
    ("origina_code", "COLXXX1_SITIO"),
    ("codigo_tsf", "COLXXX1"),
    ("sunfactory_project_id", 42),
    ("sub_project", "topico"),
])
def test_cualquiera_de_los_cuatro_cuenta(campo, valor):
    assert _hay(_candidato(**{campo: valor}), _proyecto()) is True


def test_un_candidato_sin_identificadores_no_aporta_nada():
    """Viene sólo con un nombre: no hay vínculo que registrar."""
    assert _hay(_candidato(nombre_raw="Planta X"), _proyecto()) is False


def test_no_cuenta_lo_que_el_proyecto_ya_tiene_igual():
    """Mismo valor en los dos lados: `_actualizar_desde_pendiente` tampoco
    escribiría, porque sólo rellena lo que está en None."""
    candidato = _candidato(sub_project="chima")
    proyecto = _proyecto(sub_project="chima")

    assert _hay(candidato, proyecto) is False


# ── Cómo se usa ─────────────────────────────────────────────────────────────


def test_la_condicion_entra_en_necesita_actualizar():
    import inspect

    from apps.proyectos.services import pendientes

    fuente = inspect.getsource(pendientes.resolver_pendientes)

    assert 'confianza == "nombre" and _hay_vinculo_por_escribir(c, match)' in fuente


def test_mira_los_mismos_campos_que_el_backfill():
    """Si `_actualizar_desde_pendiente` dejara de rellenar uno de estos, o
    empezara a rellenar otro, esta comprobación quedaría desalineada y el bucle
    podría volver por otro campo."""
    import inspect

    from api.v1.proyectos.views import ProyectoViewSet
    from apps.proyectos.services.pendientes import _IDENTIFICADORES_DEL_VINCULO

    fuente = inspect.getsource(ProyectoViewSet._actualizar_desde_pendiente)

    for campo in _IDENTIFICADORES_DEL_VINCULO:
        assert f'"{campo}"' in fuente, f"{campo} ya no se rellena al confirmar"


def test_no_mira_campos_que_costarian_una_consulta():
    """`municipio`, `departamento`, `latitud` y `longitud` también se rellenan
    al confirmar, pero no vienen en el `.only()` del queryset: leerlos sería una
    consulta por proyecto. Y confirmar un vínculo es sobre el vínculo."""
    from apps.proyectos.services.pendientes import _IDENTIFICADORES_DEL_VINCULO

    for campo in ("municipio", "departamento", "latitud", "longitud"):
        assert campo not in _IDENTIFICADORES_DEL_VINCULO
