"""Lo que DEBE entrar por proyecto contra lo que se liquidó en despachos.

Dos números que hoy viven en sistemas distintos y nadie cruza:

  * **Debe ingresar**: lo calcula Facturación acá, contrato por contrato
    (kWh del despacho de XM × tarifa PPA indexada), más la energía sin PPA
    valorizada a precio de bolsa.
  * **Liquidado en despachos**: lo que la API de Liquidaciones ya liquidó,
    en `market_settlements`, sumando **todos** los contratos del proyecto.

La definición del segundo la fijó Jessica y no es la del Panel Contable:

  * cuenta lo vendido —`dispatch` (contrato) y `dispatch_fazni`, que es como
    la API marca la **venta en bolsa**—;
  * y **no resta las compras en bolsa**. El Panel sí las resta, porque ahí el
    ingreso bruto es neto de lo que salió; acá la pregunta es cuánta plata se
    liquidó a favor del proyecto, y una compra no la disminuye.

El cruce es por proyecto: Facturación conoce el `proyecto_id`, la API los
nombra por `nombre_topico`. Un lado sin el otro **igual sale en la lista** —un
proyecto que facturó y no aparece liquidado es justamente lo que este cruce
busca destapar, y esconderlo lo volvería inútil.
"""


def _comparar():
    from apps.facturacion.services.vs_despachos import comparar

    return comparar


def _fila(**extra):
    """Una línea de `calculo.periodo()['lineas']`, facturable salvo que se diga."""
    base = {
        "contrato": "84962", "proyecto_id": 1, "proyecto": "El Molino",
        "kwh": 100.0, "facturacion": 500.0, "estado": "ok",
    }
    return {**base, **extra}


def _desp(**extra):
    """Una fila cruda de `market_settlements`."""
    base = {
        "project": "elmolino", "data_type": "dispatch",
        "energy": 100.0, "price": 500.0,
    }
    return {**base, **extra}


TOPICOS = {"elmolino": {"id": 1, "nombre": "El Molino"}}


def test_cruza_facturacion_y_despachos_del_mismo_proyecto():
    filas = _comparar()([_fila()], [_desp()], proyectos_por_topico=TOPICOS)
    assert len(filas) == 1
    fila = filas[0]
    assert fila["proyecto_id"] == 1
    assert fila["debe_ingresar"] == 500.0
    assert fila["liquidado_despachos"] == 500.0
    assert fila["diferencia"] == 0.0


def test_la_venta_en_bolsa_suma_igual_que_el_despacho():
    """`dispatch_fazni` es la venta en bolsa: las dos cuentan."""
    filas = _comparar()(
        [],
        [_desp(price=500.0), _desp(data_type="dispatch_fazni", price=300.0)],
        proyectos_por_topico=TOPICOS,
    )
    assert filas[0]["liquidado_despachos"] == 800.0


def test_las_compras_en_bolsa_no_se_restan():
    """La regla que separa este cruce del Panel Contable."""
    filas = _comparar()(
        [],
        [_desp(price=500.0), _desp(data_type="purchase", price=200.0)],
        proyectos_por_topico=TOPICOS,
    )
    assert filas[0]["liquidado_despachos"] == 500.0
    # Se reporta aparte: que no se reste no significa que no exista.
    assert filas[0]["compras_bolsa"] == 200.0


def test_una_compra_en_negativo_se_reporta_en_positivo():
    """La API manda el signo de la compra según el proyecto; no es confiable."""
    filas = _comparar()(
        [], [_desp(data_type="purchase", price=-200.0)], proyectos_por_topico=TOPICOS,
    )
    assert filas[0]["compras_bolsa"] == 200.0


def test_suma_todos_los_contratos_del_proyecto():
    """«Sumado de todos los contratos», dicho por Jessica."""
    filas = _comparar()(
        [_fila(contrato="A", facturacion=500.0),
         _fila(contrato="B", facturacion=250.0)],
        [_desp(contract_code="A", price=500.0),
         _desp(contract_code="B", price=250.0)],
        proyectos_por_topico=TOPICOS,
    )
    assert len(filas) == 1
    assert filas[0]["debe_ingresar"] == 750.0
    assert filas[0]["liquidado_despachos"] == 750.0


def test_la_energia_sin_ppa_se_valoriza_a_bolsa_y_suma_al_total():
    """Sin PPA no hay tarifa, pero esa energía sí se vende y sí debe entrar."""
    filas = _comparar()(
        [_fila(estado="sin_ppa", facturacion=None, kwh=200.0)],
        [], bolsa=3.5, proyectos_por_topico=TOPICOS,
    )
    assert filas[0]["ingreso_bolsa"] == 700.0
    assert filas[0]["debe_ingresar_ppa"] == 0.0
    assert filas[0]["debe_ingresar"] == 700.0


def test_sin_precio_de_bolsa_cargado_esa_energia_queda_en_cero():
    """No se inventa un precio: la usuaria carga el suyo cada mes."""
    filas = _comparar()(
        [_fila(estado="sin_ppa", facturacion=None, kwh=200.0)],
        [], bolsa=None, proyectos_por_topico=TOPICOS,
    )
    assert filas[0]["ingreso_bolsa"] == 0.0
    assert filas[0]["kwh_sin_valorizar"] == 200.0


def test_una_linea_que_no_se_puede_valorizar_se_avisa():
    """Sin tarifa o sin IPP el valor sería cero, y un cero se lee como «no vendió»."""
    filas = _comparar()(
        [_fila(estado="sin_tarifa", facturacion=None, kwh=80.0)],
        [], proyectos_por_topico=TOPICOS,
    )
    assert filas[0]["debe_ingresar"] == 0.0
    assert filas[0]["kwh_sin_valorizar"] == 80.0
    assert filas[0]["lineas_sin_valorizar"] == 1


def test_un_proyecto_que_facturo_y_no_tiene_despachos_sale_igual():
    filas = _comparar()([_fila()], [], proyectos_por_topico=TOPICOS)
    assert filas[0]["liquidado_despachos"] == 0.0
    assert filas[0]["diferencia"] == 500.0


def test_un_proyecto_liquidado_que_no_facturo_sale_igual():
    filas = _comparar()([], [_desp()], proyectos_por_topico=TOPICOS)
    assert filas[0]["debe_ingresar"] == 0.0
    assert filas[0]["diferencia"] == -500.0


def test_un_topico_que_no_cruza_con_ningun_proyecto_conserva_su_nombre():
    """Si el tópico no está mapeado, se muestra crudo en vez de perderse."""
    filas = _comparar()([], [_desp(project="desconocido")], proyectos_por_topico=TOPICOS)
    assert filas[0]["proyecto_id"] is None
    assert filas[0]["proyecto"] == "desconocido"
    assert filas[0]["liquidado_despachos"] == 500.0


def test_una_linea_sin_proyecto_se_agrupa_por_su_contrato():
    """Un SIC sin dueño no tiene proyecto; se muestra por contrato, no se descarta."""
    filas = _comparar()(
        [_fila(proyecto_id=None, proyecto=None, contrato="90060")],
        [], proyectos_por_topico=TOPICOS,
    )
    assert filas[0]["proyecto_id"] is None
    assert "90060" in filas[0]["proyecto"]
    assert filas[0]["debe_ingresar"] == 500.0


def test_ordena_por_lo_que_debe_entrar():
    filas = _comparar()(
        [_fila(proyecto_id=1, proyecto="Chico", facturacion=100.0),
         _fila(proyecto_id=2, proyecto="Grande", facturacion=900.0)],
        [], proyectos_por_topico=TOPICOS,
    )
    assert [f["proyecto"] for f in filas] == ["Grande", "Chico"]


def test_los_valores_vienen_redondeados_a_dos_decimales():
    """Van a un Excel de plata: sin redondear salen 12 decimales."""
    filas = _comparar()(
        [_fila(facturacion=0.1), _fila(facturacion=0.2)],
        [], proyectos_por_topico=TOPICOS,
    )
    assert filas[0]["debe_ingresar"] == 0.3


def test_sin_datos_no_hay_filas():
    assert _comparar()([], [], proyectos_por_topico=TOPICOS) == []
