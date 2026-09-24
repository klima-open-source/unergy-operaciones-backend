"""GOLDEN del catálogo de grupos/subservicios/tarifas (`apps.contratos.services.grupos`).

Red de seguridad para el corte a la tabla única. `grupos.py` deriva los subservicios
de un contrato de servicio de qué tarifas tiene cargadas (`tarifa_representacion`,
`tarifa_cgm`, `tarifa_base`) y de `servicio_aplica`, y mapea cada subservicio a su
columna. Son funciones PURAS sobre los atributos del contrato: cuando `ContratoServicio`
pase a ser una fachada proxy sobre `contratos`, esos atributos siguen siendo columnas,
así que estas respuestas deben quedar idénticas. Si el corte mueve una, el test lo caza.
"""
from types import SimpleNamespace

from apps.contratos.services import grupos


def _srv(**kw):
    """Stub de ContratoServicio: solo los atributos que grupos.py lee."""
    base = dict(servicio_aplica=None, tarifa_representacion=None, tarifa_cgm=None,
                tarifa_base=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_golden_subservicios_representacion_cgm():
    # Representación + CGM en la misma fila: dos subservicios (derivados de las tarifas).
    c = _srv(servicio_aplica="representacion", tarifa_representacion=6.0, tarifa_cgm=5.0)
    assert grupos.subservicios_de(c) == ["representacion", "cgm"]
    assert grupos.tarifas_de(c) == {"representacion": 6.0, "cgm": 5.0}
    assert grupos.grupo_de_contrato(c) == "representacion_cgm"

    # Solo representación cargada.
    c = _srv(servicio_aplica="representacion", tarifa_representacion=6.0)
    assert grupos.subservicios_de(c) == ["representacion"]

    # servicio_aplica='cgm' pero solo tarifa_cgm cargada.
    c = _srv(servicio_aplica="cgm", tarifa_cgm=5.0)
    assert grupos.subservicios_de(c) == ["cgm"]

    # Sin tarifas: cae a servicio_aplica para no desaparecer de su pestaña.
    c = _srv(servicio_aplica="representacion")
    assert grupos.subservicios_de(c) == ["representacion"]


def test_golden_subservicios_operacion():
    for sa in ("mantenimiento", "arriendo", "internet"):
        c = _srv(servicio_aplica=sa, tarifa_base=100.0)
        assert grupos.subservicios_de(c) == [sa]
        assert grupos.grupo_de_contrato(c) == "operacion"
        # Los tres de operación comparten tarifa_base.
        assert grupos.tarifa_de(c, sa) == 100.0

    # servicio_aplica desconocido -> lista vacía (no inventa grupo).
    assert grupos.subservicios_de(_srv(servicio_aplica="otro")) == []


def test_golden_tarifa_de_por_columna():
    c = _srv(tarifa_representacion=6.0, tarifa_cgm=5.0, tarifa_base=100.0)
    assert grupos.tarifa_de(c, "representacion") == 6.0
    assert grupos.tarifa_de(c, "cgm") == 5.0
    assert grupos.tarifa_de(c, "mantenimiento") == 100.0
    assert grupos.tarifa_de(c, "arriendo") == 100.0
    assert grupos.tarifa_de(c, "internet") == 100.0
    assert grupos.tarifa_de(c, "desconocido") is None


def test_golden_subservicio_de_ppa():
    assert grupos.subservicio_de_ppa(SimpleNamespace(tipo_contrato="compra")) == "compra"
    assert grupos.subservicio_de_ppa(SimpleNamespace(tipo_contrato="venta")) == "venta"
    # Nulo se trata como venta (default de la columna).
    assert grupos.subservicio_de_ppa(SimpleNamespace(tipo_contrato=None)) == "venta"


def test_golden_grupo_de_y_catalogo():
    assert grupos.grupo_de("compra") == "ppa"
    assert grupos.grupo_de("representacion") == "representacion_cgm"
    assert grupos.grupo_de("cgm") == "representacion_cgm"
    assert grupos.grupo_de("mantenimiento") == "operacion"
    assert grupos.grupo_de("arriendo") == "operacion"
    assert grupos.grupo_de("internet") == "operacion"
    assert grupos.grupo_de(None) is None

    assert grupos.catalogo() == [
        {"grupo": "ppa", "subservicios": ["compra", "venta"]},
        {"grupo": "representacion_cgm", "subservicios": ["representacion", "cgm"]},
        {"grupo": "operacion", "subservicios": ["mantenimiento", "arriendo", "internet"]},
    ]
