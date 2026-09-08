"""Pruebas de la fusion de contratos de representacion duplicados.

El caso que manda es MGS Naos 2: tres filas para un solo contrato, una por cada
fuente que escribio la tabla. Y el contraejemplo es Baraya, que tiene tres
inversionistas de verdad y donde fusionar seria destruir datos.
"""
from types import SimpleNamespace

from app.services.representacion_dedup import agrupar, analizar, norm, revisar


def reg(id, inv=None, pid=None, ref=None, sf=None, numero=None, **kw):
    base = dict(
        id=id, inversionista_nombre=inv, proyecto_id=pid, nombre_proyecto_ref=ref,
        codigo_sun_factory=sf, numero_contrato=numero,
        contratante_nombre=None, contratante_nit=None, contratante_id=None,
        prestador_nombre=None, prestador_nit=None, prestador_id=None,
        portafolio=None, fecha_inicio=None, fecha_fin=None,
        fecha_firma_contrato=None, fecha_indexacion=None, fecha_inicio_om=None,
        renovacion_automatica=None, periodicidad_pago=None, indice_indexacion=None,
        tarifa_base=None, tarifa_mensual=None,
        tarifa_admin=None, tarifa_cgm=None, tarifa_representacion=None,
        indexacion_cgm=None, indexacion_representacion=None,
        enlace_drive=None, estado_pago=None, estado="vigente",
        cgm_codigo_sic=None,
        responsable_iva=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── MGS Naos 2: las tres fuentes, un solo contrato ───────────────────────────
NAOS_SEED_VIEJO = reg(101, inv="GD EL REMOLINO 1 S.A.S. E.S.P", pid=3,
                      tarifa_cgm=7.0, tarifa_representacion=3.0)
NAOS_SEED_NUEVO = reg(102, inv="GD EL REMOLINO 1 S.A.S. E.S.P", pid=3,
                      ref="MGS Naos 2", tarifa_cgm=7.0, tarifa_representacion=3.0,
                      fecha_firma_contrato="2025-02-20")
NAOS_WIZARD = reg(103, pid=3, numero="UNERGY-RC-002-2025",
                  fecha_inicio="2025-02-20", fecha_fin="2026-04-30",
                  estado="terminado", enlace_drive="https://drive/x")


class TestAgrupar:
    def test_naos_junta_las_tres_filas(self):
        grupos = agrupar([NAOS_SEED_VIEJO, NAOS_SEED_NUEVO, NAOS_WIZARD])
        assert len(grupos) == 1
        assert sorted(r.id for r in grupos[0]) == [101, 102, 103]

    def test_el_del_wizard_entra_porque_la_planta_tiene_un_solo_inversionista(self):
        grupos = agrupar([NAOS_SEED_NUEVO, NAOS_WIZARD])
        assert sorted(r.id for r in grupos[0]) == [102, 103]

    def test_el_del_wizard_queda_fuera_si_la_planta_tiene_varios_inversionistas(self):
        """Baraya: con tres inversionistas no se puede saber a cual pertenece."""
        a = reg(1, inv="Solenium S.A.S", pid=9)
        b = reg(2, inv="Unergy S.A.S", pid=9)
        wizard = reg(3, pid=9, numero="ALGO-001")
        grupos = agrupar([a, b, wizard])
        # a y b son inversionistas distintos: ningun grupo de duplicados, y el
        # del wizard no se pega a ninguno.
        assert grupos == []

    def test_inversionistas_distintos_no_se_agrupan(self):
        a = reg(1, inv="Solenium S.A.S", pid=9)
        b = reg(2, inv="Unergy S.A.S", pid=9)
        assert agrupar([a, b]) == []

    def test_la_tilde_no_separa(self):
        a = reg(1, inv="SOMOS BOGOTÁ USME SAS", pid=5)
        b = reg(2, inv="SOMOS BOGOTA USME SAS", pid=5)
        assert len(agrupar([a, b])) == 1

    def test_sin_planta_ni_inversionista_se_ignora(self):
        assert agrupar([reg(1), reg(2)]) == []

    def test_agrupa_huerfanos_por_nombre_de_referencia(self):
        """Dos filas sin proyecto_id pero del mismo contrato."""
        a = reg(1, inv="Ayurá S.A.S.", ref="Minigranja 0040 - La Cacica")
        b = reg(2, inv="Ayura S.A.S.", ref="MINIGRANJA 0040 LA CACICA")
        assert len(agrupar([a, b])) == 1


class TestTresNaos:
    """El caso real: GD NAOS 1, MGS Naos 2 y MGS Naos 3 son TRES plantas, cada
    una con su contrato con el mismo inversionista, y cada una duplicada por los
    dos seeds. El registro del wizard existe solo en Naos 2.

    Lo que no debe pasar: que las tres se mezclen en un grupo por compartir
    inversionista, ni que el registro del wizard se pegue a la planta equivocada.
    """

    def contratos(self):
        return [
            # GD NAOS 1 (planta 19)
            reg(201, inv="GD EL REMOLINO 1 S.A.S. E.S.P", pid=19, tarifa_cgm=7.0),
            reg(202, inv="GD EL REMOLINO 1 S.A.S. E.S.P", pid=19, ref="GD NAOS 1"),
            # MGS Naos 2 (planta 25) + el del wizard
            reg(203, inv="GD EL REMOLINO 1 S.A.S. E.S.P", pid=25, tarifa_cgm=7.0),
            reg(204, inv="GD EL REMOLINO 1 S.A.S. E.S.P", pid=25, ref="MGS Naos 2"),
            reg(205, pid=25, numero="UNERGY-RC-002-2025", fecha_fin="2026-04-30"),
            # MGS Naos 3 (planta 26)
            reg(206, inv="GD EL REMOLINO 1 S.A.S. E.S.P", pid=26, tarifa_cgm=7.0),
            reg(207, inv="GD EL REMOLINO 1 S.A.S. E.S.P", pid=26, ref="MGS Naos 3"),
        ]

    def test_son_tres_grupos_uno_por_planta(self):
        grupos = agrupar(self.contratos())
        assert len(grupos) == 3
        por_planta = {g[0].proyecto_id: sorted(r.id for r in g) for g in grupos}
        assert por_planta == {19: [201, 202], 25: [203, 204, 205], 26: [206, 207]}

    def test_el_del_wizard_solo_cae_en_su_planta(self):
        grupos = agrupar(self.contratos())
        con_wizard = [g for g in grupos if any(r.id == 205 for r in g)]
        assert len(con_wizard) == 1
        assert con_wizard[0][0].proyecto_id == 25

    def test_las_tres_se_fusionan_por_separado(self):
        inf = revisar(self.contratos())
        assert len(inf["grupos_fusionables"]) == 3
        assert inf["grupos_con_conflicto"] == []
        # 7 registros -> 3 contratos: se eliminan 4
        assert inf["contratos_a_eliminar"] == 4
        # y el numero del wizard queda en el contrato de Naos 2, no en otro
        naos2 = next(g for g in inf["grupos_fusionables"] if g["proyecto_id"] == 25)
        otros = [g for g in inf["grupos_fusionables"] if g["proyecto_id"] != 25]
        assert 205 in naos2["ids"]
        for g in otros:
            valores_y_ids = str(g["valores"]) + str(g["ids"])
            assert "UNERGY-RC-002-2025" not in valores_y_ids


class TestAnalizar:
    def test_naos_es_fusionable_y_completa_los_huecos(self):
        grupo = [NAOS_SEED_VIEJO, NAOS_SEED_NUEVO, NAOS_WIZARD]
        r = analizar(grupo)
        assert r["fusionable"] is True
        assert r["conflictos"] == []
        assert len(r["eliminar"]) == 2

        # El registro resultante = lo que ya tenia el conservado + `valores`.
        conservado = next(x for x in grupo if x.id == r["conservar"])
        final = {c: r["valores"].get(c, getattr(conservado, c)) for c in
                 ("numero_contrato", "inversionista_nombre", "tarifa_cgm",
                  "tarifa_representacion", "nombre_proyecto_ref", "fecha_inicio",
                  "fecha_fin", "enlace_drive", "fecha_firma_contrato")}
        assert final == {
            "numero_contrato": "UNERGY-RC-002-2025",
            "inversionista_nombre": "GD EL REMOLINO 1 S.A.S. E.S.P",
            "tarifa_cgm": 7.0,
            "tarifa_representacion": 3.0,
            "nombre_proyecto_ref": "MGS Naos 2",
            "fecha_inicio": "2025-02-20",
            "fecha_fin": "2026-04-30",
            "enlace_drive": "https://drive/x",
            "fecha_firma_contrato": "2025-02-20",
        }

    def test_la_fusion_no_pierde_ningun_dato(self):
        """Todo valor no vacio del grupo sobrevive: o ya esta en el que se
        conserva, o entra por `valores`."""
        grupo = [NAOS_SEED_VIEJO, NAOS_SEED_NUEVO, NAOS_WIZARD]
        r = analizar(grupo)
        conservado = next(x for x in grupo if x.id == r["conservar"])
        for origen in grupo:
            for campo, valor in vars(origen).items():
                if campo == "id" or valor in (None, "", False):
                    continue
                if campo in ("estado",):      # campo blando, puede diferir
                    continue
                final = r["valores"].get(campo, getattr(conservado, campo, None))
                assert final not in (None, ""), f"se perdio {campo}={valor!r}"

    def test_conflicto_en_un_campo_de_lista_no_revienta(self):
        """MGS Naos 3: dos registros con indexaciones distintas.

        `indexacion_cgm` es una lista de dicts. Deduplicar los valores en
        conflicto con `set`/`dict.fromkeys` lanzaba TypeError (una lista no es
        hashable) y el endpoint devolvia 500, asi que el aviso de duplicados
        nunca aparecia en la ficha.
        """
        a = reg(60, inv="GD EL REMOLINO 1 S.A.S. E.S.P", pid=26,
                indexacion_cgm=[{"año": 2024, "valor": 7.0, "esBase": True}])
        b = reg(61, inv="GD EL REMOLINO 1 S.A.S. E.S.P", pid=26,
                indexacion_cgm=[{"año": 2025, "valor": 7.364, "ipc": 5.2}])
        r = analizar([a, b])
        assert r["fusionable"] is False
        assert [c["campo"] for c in r["conflictos"]] == ["indexacion_cgm"]
        assert len(r["conflictos"][0]["valores"]) == 2
        # y el informe completo tambien sobrevive
        assert len(revisar([a, b])["grupos_con_conflicto"]) == 1

    def test_valores_largos_se_recortan(self):
        larga = [{"año": 2000 + i, "valor": i * 1.5, "ipc": 5.0} for i in range(20)]
        a = reg(1, inv="X S.A.S", pid=4, indexacion_cgm=larga)
        b = reg(2, inv="X S.A.S", pid=4, indexacion_cgm=[{"año": 2024, "valor": 1}])
        r = analizar([a, b])
        assert all(len(v) <= 161 for v in r["conflictos"][0]["valores"])

    def test_indexaciones_iguales_no_son_conflicto(self):
        idx = [{"año": 2024, "valor": 7.0, "esBase": True}]
        a = reg(1, inv="X S.A.S", pid=4, indexacion_cgm=list(idx))
        b = reg(2, inv="X S.A.S", pid=4, indexacion_cgm=list(idx))
        assert analizar([a, b])["fusionable"] is True

    def test_conflicto_real_bloquea_la_fusion(self):
        a = reg(1, inv="X S.A.S", pid=4, numero="AAA-1", tarifa_cgm=6.0)
        b = reg(2, inv="X S.A.S", pid=4, numero="BBB-2", tarifa_cgm=6.0)
        r = analizar([a, b])
        assert r["fusionable"] is False
        assert [c["campo"] for c in r["conflictos"]] == ["numero_contrato"]
        assert sorted(r["conflictos"][0]["valores"]) == ["AAA-1", "BBB-2"]

    def test_tarifas_distintas_son_conflicto(self):
        a = reg(1, inv="X S.A.S", pid=4, tarifa_cgm=6.0)
        b = reg(2, inv="X S.A.S", pid=4, tarifa_cgm=7.0)
        assert analizar([a, b])["fusionable"] is False

    def test_estado_distinto_no_bloquea(self):
        """El seed pone 'vigente' por defecto; que otra fila diga 'terminado' no
        es una contradiccion que deba frenar la fusion."""
        a = reg(1, inv="X S.A.S", pid=4, estado="vigente")
        b = reg(2, inv="X S.A.S", pid=4, estado="terminado")
        assert analizar([a, b])["fusionable"] is True

    def test_mismo_valor_escrito_distinto_no_es_conflicto(self):
        a = reg(1, inv="X S.A.S", pid=4, contratante_nombre="Unergy Energía Digital S.A.S. E.S.P.")
        b = reg(2, inv="X S.A.S", pid=4, contratante_nombre="UNERGY ENERGIA DIGITAL SAS ESP")
        assert analizar([a, b])["fusionable"] is True

    def test_eleccion_estable_a_igualdad_de_datos(self):
        a, b = reg(7, inv="X S.A.S", pid=4), reg(3, inv="X S.A.S", pid=4)
        assert analizar([a, b])["conservar"] == 3          # el id mas bajo
        assert analizar([b, a])["conservar"] == 3          # sin importar el orden


class TestRevisar:
    def test_informe_separa_lo_limpio_de_lo_dudoso(self):
        limpio_a = reg(1, inv="X S.A.S", pid=4, tarifa_cgm=6.0)
        limpio_b = reg(2, inv="X S.A.S", pid=4, ref="Planta X")
        chocan_a = reg(5, inv="Y S.A.S", pid=8, numero="AAA")
        chocan_b = reg(6, inv="Y S.A.S", pid=8, numero="BBB")
        inf = revisar([limpio_a, limpio_b, chocan_a, chocan_b])
        assert len(inf["grupos_fusionables"]) == 1
        assert len(inf["grupos_con_conflicto"]) == 1
        assert inf["contratos_a_eliminar"] == 1
        assert inf["grupos_con_conflicto"][0]["ids"] == [5, 6]

    def test_sin_duplicados_informe_vacio(self):
        inf = revisar([reg(1, inv="X S.A.S", pid=1), reg(2, inv="Y S.A.S", pid=2)])
        assert inf["grupos_fusionables"] == []
        assert inf["contratos_a_eliminar"] == 0


def test_norm():
    assert norm("Ayurá  S.A.S.") == norm("AYURA S A S")
    assert norm(None) == ""
