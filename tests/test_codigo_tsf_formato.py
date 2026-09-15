"""El Código TSF que se escribe a mano tiene que ser el prefijo, no el completo.

Sun Factory manda un solo dato, el `base_name` (`COLCEST924P3_ASTREA_ORIENTE`),
y de ahí salen dos campos nuestros: `origina_code` se queda con el entero y
`codigo_tsf` con el prefijo CREG (`COLCEST924P3`).

Pero `codigo_tsf` también es un campo del formulario, y hasta ahora nadie
validaba el formato. Tres proyectos --las tres Astrea (Calipso, Rigel y Titán)--
quedaron con el `base_name` completo ahí y `origina_code` vacío. Eso rompe el
emparejamiento de `tsf_sync`, que cruza por el prefijo para reconocer un
proyecto que ya existe: hay una línea en esa sincronización que también prueba
`Q(codigo_tsf=base_name)`, un parche puesto justo por este caso.

Y no es anecdótico: Astrea 1 fue el duplicado (ids 274 y 275) que motivó la
restricción única de `codigo_tsf`.

**Se rechaza en vez de recortar en silencio.** Quien pega el valor completo
probablemente quiso registrar el proyecto de Sun Factory; decírselo --con el
prefijo ya calculado en el mensaje-- es más útil que guardar la mitad sin
avisar.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _validar(valor):
    from api.v1.proyectos.serializers import ProyectoCrearSerializer

    return ProyectoCrearSerializer().validate_codigo_tsf(valor)


def _error(valor):
    from rest_framework.exceptions import ValidationError

    with pytest.raises(ValidationError) as exc:
        _validar(valor)
    return str(exc.value)


# ── Lo que se acepta ────────────────────────────────────────────────────────


@pytest.mark.parametrize("codigo", [
    "COLCEST924P3", "COLSUCT17P2", "COLANTT57P1", "COLNORT31P1",
])
def test_un_prefijo_creg_pasa(codigo):
    assert _validar(codigo) == codigo


def test_se_le_quitan_los_espacios():
    assert _validar("  COLCEST924P3  ") == "COLCEST924P3"


@pytest.mark.parametrize("vacio", [None, ""])
def test_vacio_pasa(vacio):
    """67 de 188 proyectos no tienen código: el campo es opcional."""
    assert _validar(vacio) == vacio


# ── Lo que se rechaza ───────────────────────────────────────────────────────


def test_el_base_name_completo_se_rechaza():
    """El caso Astrea, que es el que motivó todo esto."""
    mensaje = _error("COLCEST924P3_ASTREA_ORIENTE")

    assert "COLCEST924P3" in mensaje, "el mensaje debe traer el prefijo ya calculado"


def test_el_mensaje_explica_que_paso():
    """Un "formato inválido" a secas no le dice a nadie qué hacer."""
    mensaje = _error("COLSUCT17P2_GALERAS_SUR")

    assert "Sun Factory" in mensaje
    assert "COLSUCT17P2" in mensaje


@pytest.mark.parametrize("basura", [
    "no es un codigo", "123456", "ABCDEF1", "COL CEST924P3", "_COLCEST924P3",
])
def test_lo_que_no_se_parece_a_nada_tambien_se_rechaza(basura):
    assert "CREG" in _error(basura) or "Sun Factory" in _error(basura)


def test_no_recorta_en_silencio():
    """Lo importante: NO devuelve el prefijo. Guardar la mitad sin avisar
    esconde que quien escribió quería otra cosa."""
    from rest_framework.exceptions import ValidationError

    with pytest.raises(ValidationError):
        _validar("COLCEST924P3_ASTREA_ORIENTE")


# ── Que aplique también al editar ───────────────────────────────────────────


def test_el_serializer_de_edicion_hereda_la_validacion():
    """`ProyectoActualizarSerializer` extiende al de creación: si algún día deja
    de heredarlo, el PATCH volvería a aceptar cualquier cosa."""
    from rest_framework.exceptions import ValidationError

    from api.v1.proyectos.serializers import ProyectoActualizarSerializer

    with pytest.raises(ValidationError):
        ProyectoActualizarSerializer().validate_codigo_tsf("COLCEST924P3_ASTREA_ORIENTE")


def test_el_del_crm_tambien():
    from rest_framework.exceptions import ValidationError

    from api.v1.proyectos.serializers import ProyectoDesdeCrmSerializer

    with pytest.raises(ValidationError):
        ProyectoDesdeCrmSerializer().validate_codigo_tsf("COLCEST924P3_ASTREA_ORIENTE")


# ── El comando de limpieza ──────────────────────────────────────────────────


def test_el_comando_no_escribe_sin_pedirselo():
    import inspect

    from apps.proyectos.management.commands import revisar_codigo_tsf

    fuente = inspect.getsource(revisar_codigo_tsf)

    assert 'if not opciones["ejecutar"]:' in fuente
    assert "Nada escrito" in fuente


def test_el_comando_no_pisa_un_origina_code_ocupado():
    """Si ya hay un valor distinto, son dos datos y elegir no es cosa de un
    comando."""
    import inspect

    from apps.proyectos.management.commands import revisar_codigo_tsf

    fuente = inspect.getsource(revisar_codigo_tsf)

    assert "bloqueados" in fuente
    assert "actual_origina and actual_origina != p.codigo_tsf" in fuente
