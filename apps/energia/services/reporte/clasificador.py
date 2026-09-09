"""Árbol de decisión: asigna Caso 0-8 a cada frontera de Generación para un día.

Puerto de process/src/internals/clasificador.py (repo Reporte-Energia).

Diferencias con el pipeline original:
  - El CGM se lee con GaiaClient.get_border_report_status(border_id, fecha),
    que ya filtra por la fecha exacta -- así que 'reporte_valido' se
    simplifica a "hubo respuesta para esa fecha Y su status es OK/WARNING",
    sin necesitar comparar contra un 'last_report_date' aparte.
  - Recuperación activa de medidor (interrogación WebSocket a Quoia, ver
    app/services/reporte_energia/recuperacion.py) ya está portada -- vive
    dentro de curvas.curvas_de_frontera(), que la dispara automáticamente
    cuando la lectura pasiva de un medidor sale incompleta, antes de que su
    resultado llegue a este árbol de decisión.

Nota sobre nombres: 'e_cgm' (reporte oficial de Quoia/ASIC) y 'e_nodo' (curva
del nodo de monitoreo en tiempo real) son dos fuentes físicas distintas que
pueden no coincidir en el tiempo. Si el reporte automático de Quoia es
válido para ese día ('reporte_valido'), se valida/reporta con e_cgm (Casos 1,
5). Si no, e_cgm deja de ser la referencia -- se valida con e_nodo vs
inversores (Casos 2/3/4, 5).

Corroboración del medidor (2026-09-02): si el reporte automático no cuadra
contra inversores, antes de bajar a Casos 2/3/4 se le da al CGM una segunda
oportunidad contra el MEDIDOR (fuente física independiente): si CGM y un
medidor completo coinciden dentro del rango Y el CGM es mayor que los
inversores -- la firma de un SolarView desactualizado --, se resuelve como
Caso 1 (se confía en el reporte que Quoia ya envió, sin matriz ni revisión
manual). Motivo: los inversores son hoy la fuente menos confiable, y hacerlos
el único validador del CGM mandaba a revisión casos sin evidencia real contra
el CGM.

TEMPORAL (2026-09-02): todo lo que termine con 'medidor_usado' == 'inversores'
(el "Inversores × FP" de la plataforma) sale con 'revisar_manualmente' = True,
sin importar el Caso. Motivo: la generación de Solenium a veces llega
desactualizada aunque el día figure completo, y el total de inversores es justo
la base de ese reporte. Aplica a Caso 3 y al fallback de Caso 5 (que ya lo
marcaba por su propio motivo). QUITAR cuando Solenium se normalice -- es
contención, no una regla del árbol.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from app.services.mgs.gaia_client import GaiaClient
from app.services.mgs.solarview_client import SolarViewClient
from apps.energia.services.reporte import curvas, datos_crudos, solarview as solarview_svc, reconectador, historial
from apps.energia.services.reporte.utils import (
    CURVA_CERO, CURVA_VACIA, HORAS_SOLARES, escalar_curva, escalar_curva_con_huecos, curva_a_lista,
    limite_plausible_kwh,
)

HORAS = list(range(24))

CASOS_CON_RELLENO_HORARIO = {2, 3, 4, 5, 7, 8}
RANGO_ERROR         = 6.0               # %: error aceptable [-6%, +6%]
# Tolerancia para que el MEDIDOR corrobore un CGM que no cuadró contra
# inversores (ver la corroboración en _decidir_caso). Más estricta que
# RANGO_ERROR a propósito, por dos motivos:
#   · CGM y medidor de nodo son el MISMO aparato leído por dos canales, así que
#     deben coincidir mucho mejor que dos fuentes independientes (en el caso que
#     motivó la regla, Chiriguaná Norte 1, coincidían al decimal: 0,00%).
#   · Al ser más estricta que RANGO_ERROR, garantiza separación: si se llega a
#     la corroboración es porque los inversores están a MÁS de 6%, así que el
#     medidor queda al menos 4 puntos más cerca del CGM. Con la tolerancia de
#     6% podían quedar casi empatados (medidor -5,9% vs inversores -7%), que es
#     medidor e inversores coincidiendo ENTRE SÍ contra el CGM -- lo contrario
#     de una corroboración (2026-09-02).
RANGO_CORROBORACION = 2.0               # %: CGM vs medidor para confiar en el CGM
ESTADOS_AUTOMATICO  = {"OK", "WARNING"}  # estados en que el reporte ASIC de hoy es válido

# frontera_id de proyectos sin inversores (nunca registrados en Solenium)
# cuyo medidor de nodo se sabe que sub-reporta crónicamente frente a la
# energía real, sin forma de calcular un factor de corrección. Confirmar
# los ids reales contra la BD ("GD Agustín 2" en el pipeline original).
MEDIDORES_SIN_INVERSOR_SOSPECHOSOS: set[int] = set()

# frontera_id de proyectos cuyo CGM lo hace al ASIC otra empresa distinta a
# Unergy -- no aplica este árbol de Casos. El medidor de nodo de Quoia no
# tiene telemetría (confirmado en vivo: energia_medidor_principal/respaldo_kwh
# siempre 0), así que mientras no se suba el Excel del tercero para el día
# (POST /fronteras/{id}/cargar-excel-terceros) queda en caso=0/"externo" con
# revisar_manualmente=True -- ver clasificar_generacion() más abajo.
# 79 = Complejo Industrial Cedillanos (Frt88292).
FRONTERAS_TERCEROS: set[int] = {79}


def _en_rango(error: float | None) -> bool:
    return error is not None and abs(error) <= RANGO_ERROR


def _corrobora(error: float | None) -> bool:
    """El medidor confirma el CGM (tolerancia estricta, ver RANGO_CORROBORACION)."""
    return error is not None and abs(error) <= RANGO_CORROBORACION


def _error_con_curva(e_inv: float, curva: pd.Series) -> float | None:
    if e_inv == 0:
        return None
    e_nodo = curva.fillna(0).sum()
    return (e_inv - e_nodo) / e_inv * 100


def _error_ventana_solar(curva_referencia: pd.Series, curva_solarview: pd.Series) -> float | None:
    """Compara curva_referencia (CGM o medidor) contra SolarView, ambas
    restringidas a la ventana solar (HORAS_SOLARES) -- las horas que
    SolarView no reportó DENTRO de esa ventana cuentan como 0 en la
    comparación (no se excluyen, a diferencia de las horas fuera de la
    ventana, que nunca importan). Antes se comparaba solo contra las horas
    que Solenium sí tenía, sin importar dónde cayeran -- si esas horas eran
    un pedazo angosto y no representativo del día (ver MGS Gandalf
    2026-08-10: Solenium solo reportó 12h-23h, sin la mañana completa), la
    comparación pasaba "bien" sin haber validado nada real. Con esta
    ventana fija, un hueco de generación real que SolarView se perdió sí se
    nota (ver MGS 0025 El Copey Occidente 2026-08-05, que sigue pasando
    limpio -- su hueco real era de madrugada, fuera de la ventana)."""
    ref_ventana = float(curva_referencia.reindex(HORAS_SOLARES).fillna(0).sum())
    sol_ventana = float(curva_solarview.reindex(HORAS_SOLARES).fillna(0).sum())
    if sol_ventana == 0:
        return None
    return (sol_ventana - ref_ventana) / sol_ventana * 100


def _tiene_dato(curva: pd.Series | None) -> bool:
    return isinstance(curva, pd.Series) and curva.notna().any()


def _mejor_medidor(curva_a: pd.Series, curva_b: pd.Series) -> pd.Series:
    """Entre dos curvas de medidor, la de mayor energía total -- cubre a la
    vez "solo una tiene dato" y "ambas reportan, hay que elegir" (convención:
    se prefiere el de mayor valor cuando no hay CGM ni inversores contra qué
    validar). Usada SOLO como referencia para calcular el error vs inversores
    (Casos 2/3/4) -- ahí sí importa la magnitud, porque decide si el día es
    Caso 3 o Caso 4. NO usar para decidir qué medidor reportar directamente
    (ver _principal_o_respaldo)."""
    return curva_a if curva_a.fillna(0).sum() >= curva_b.fillna(0).sum() else curva_b


def _principal_o_respaldo(curva_ppal: pd.Series, curva_resp: pd.Series) -> pd.Series:
    """Para reportar un medidor directo sin nada contra qué validar --
    prefiere SIEMPRE el principal si tiene dato; el respaldo solo si el
    principal no tiene nada. No el de mayor valor (decisión del usuario tras
    ver Sol&Cielo 7 Los Bongos Consumo 2026-08-03, mismo criterio aplicado
    ahí en clasificador_consumo.py)."""
    return curva_ppal if _tiene_dato(curva_ppal) else curva_resp


def _decidir_caso(
    frontera_id: int,
    fecha: date,
    fecha_str: str,
    e_cgm: float,
    curva_cgm: pd.Series,
    reporte_valido: bool,
    cgm_tiene_dato: bool,
    curva_ppal: pd.Series,
    curva_resp: pd.Series,
    completo_ppal: bool,
    completo_resp: bool,
    e_inv: float,
    e_inv_incompleto: float | None,
    curva_solarview: pd.Series,
    id_solarview: int | None,
    node_ppal: int | None,
    gaia: GaiaClient,
    sv: SolarViewClient,
    capacidad_efectiva_mw: float | None = None,
) -> dict:
    """Puerto de _clasificar_fila(). Devuelve {'caso', 'energia_final_kwh',
    'curva_final', 'medidor_usado', ...}."""

    # --- Caso 1: reporte ASIC válido hoy y error en rango ---
    if reporte_valido and e_inv > 0 and e_cgm > 0 and _en_rango(_error_con_curva(e_inv, curva_cgm)):
        return {"caso": 1, "energia_final_kwh": e_cgm, "curva_final": curva_cgm, "medidor_usado": "cgm"}

    # --- Caso 1 por corroboración del MEDIDOR (2026-09-02) ---------------------
    # El CGM automático no cuadró contra inversores, pero los inversores son hoy
    # la fuente menos confiable (SolarView/Solenium llega desactualizado aunque
    # el día figure completo). Antes esto mandaba a revisar a mano un CGM que
    # probablemente estaba bien, sin evidencia real en su contra.
    #
    # El medidor de nodo es una fuente FÍSICAMENTE INDEPENDIENTE del CGM (otro
    # canal de telecomunicaciones, ver la nota de e_cgm vs e_nodo arriba): si
    # coinciden, la discrepancia se le atribuye a los inversores. Se exige
    # además que el CGM sea MAYOR que los inversores -- la firma de un SolarView
    # desactualizado/parcial; si los inversores reportaran MÁS, eso sí sugeriría
    # que el CGM subreporta (el riesgo real para el ASIC) y se mantiene la
    # cautela. Decisión de la usuaria, 2026-09-02.
    #
    # Se resuelve como Caso 1 con medidor_usado="cgm" a propósito: mismo
    # desenlace (se confía en el reporte que Quoia ya envió), no se manda matriz
    # de generación, y queda exento de la marca por hueco de SolarView de más
    # abajo. Se exige medidor COMPLETO: uno incompleto que "cuadra" es
    # coincidencia, no validación (mismo criterio que Caso 2). Y la coincidencia
    # se mide con RANGO_CORROBORACION (más estricto que RANGO_ERROR) para que el
    # medidor de verdad respalde al CGM, en vez de estar empatado con los
    # inversores -- ver la nota de esa constante.
    #
    # `e_inv` llega en 0 cuando SolarView vino incompleto (el total parcial va en
    # e_inv_incompleto, ver el caller), así que acá se usa el que haya.
    e_inv_ref = e_inv or e_inv_incompleto or 0.0
    if reporte_valido and e_cgm > 0 and e_inv_ref > 0 and e_cgm > e_inv_ref:
        error_cgm_ppal = _error_con_curva(e_cgm, curva_ppal) if _tiene_dato(curva_ppal) else None
        error_cgm_resp = _error_con_curva(e_cgm, curva_resp) if _tiene_dato(curva_resp) else None
        if (_corrobora(error_cgm_ppal) and completo_ppal) or (_corrobora(error_cgm_resp) and completo_resp):
            # El error que se expone es el de CGM vs inversores (el que no
            # cuadró): es lo que explica por qué esta fila necesitó
            # corroboración. Con inversores incompletos se usa la ventana solar,
            # igual que el camino de Caso 5, porque el total del día completo
            # contra un total parcial siempre se vería mal.
            error_vs_inv = (
                _error_ventana_solar(curva_cgm, curva_solarview)
                if e_inv == 0 and isinstance(curva_solarview, pd.Series)
                else _error_con_curva(e_inv_ref, curva_cgm)
            )
            return {
                "caso": 1, "energia_final_kwh": e_cgm, "curva_final": curva_cgm,
                "medidor_usado": "cgm", "error_final_pct": error_vs_inv,
                "revisar_manualmente": False,
            }

    # --- Casos 2/3/4: reporte no válido, o CGM no valida contra inversores ---
    if e_inv > 0:
        error_ppal = _error_con_curva(e_inv, curva_ppal) if _tiene_dato(curva_ppal) else None
        error_resp = _error_con_curva(e_inv, curva_resp) if _tiene_dato(curva_resp) else None

        if _en_rango(error_ppal) and completo_ppal:
            return {"caso": 2, "energia_final_kwh": float(curva_ppal.fillna(0).sum()), "curva_final": curva_ppal, "medidor_usado": "principal"}
        if _en_rango(error_resp) and completo_resp:
            return {"caso": 2, "energia_final_kwh": float(curva_resp.fillna(0).sum()), "curva_final": curva_resp, "medidor_usado": "respaldo"}

        # Ningún medidor corrige -- distinguir Caso 3 (error positivo) de Caso 4 (negativo)
        curva_ref = _mejor_medidor(curva_ppal, curva_resp)
        error_ref = _error_con_curva(e_inv, curva_ref) if _tiene_dato(curva_ref) else None

        if error_ref is None or error_ref > 0:
            # Caso 3: medidores subreportan -> inversores × factor de pérdida
            fp_val, fp_calc = historial.get_factor_perdida_detalle(frontera_id, fecha)
            if fp_val is None:
                return {
                    "caso": 3, "energia_final_kwh": None, "curva_final": CURVA_VACIA.copy(),
                    "fp": None, "fp_calculada": None, "revisar_manualmente": True,
                    "medidor_usado": "revisar", "error_final_pct": error_ref,
                }
            e_fp = e_inv * fp_val
            curva_inv = escalar_curva(curva_solarview, e_fp) if isinstance(curva_solarview, pd.Series) else CURVA_CERO.copy()
            return {
                "caso": 3, "energia_final_kwh": e_fp, "curva_final": curva_inv,
                "fp": fp_val, "fp_calculada": fp_calc, "medidor_usado": "inversores",
                "error_final_pct": error_ref,
                # TEMPORAL (2026-09-02, pedido de la usuaria): todo lo que reporte
                # con inversores × FP se marca para revisión manual. La generación
                # de Solenium a veces llega desactualizada aunque el día figure
                # completo, y el total de inversores es justo la base de este Caso.
                # QUITAR cuando Solenium se normalice -- es contención, no una
                # regla del árbol de Casos. Portado de Reporte-Energia
                # (process/src/internals/clasificador.py, mismo cambio).
                "revisar_manualmente": True,
            }
        else:
            # Caso 4: medidores sobrereportan -> medidor de mayor valor
            return {
                "caso": 4, "energia_final_kwh": float(curva_ref.fillna(0).sum()), "curva_final": curva_ref,
                "medidor_usado": "principal" if curva_ref is curva_ppal else "respaldo",
            }

    # --- Caso 5: tengo medidores pero no inversores ---
    if e_inv == 0 and e_cgm > 0:
        if reporte_valido:
            resultado_cgm = {"caso": 5, "energia_final_kwh": e_cgm, "curva_final": curva_cgm, "medidor_usado": "cgm"}
            # SolarView reportó parcial ese día (e_inv_incompleto) -- no se
            # descarta solo por estar incompleto, se usa igual como chequeo
            # de plausibilidad: se compara CGM contra inversores dentro de
            # la ventana solar (huecos de SolarView ahí cuentan como 0, ver
            # _error_ventana_solar) -- no el total del día completo, que
            # siempre se vería "mal" contra un total parcial. Si coincide
            # dentro del rango normal, no hace falta Revisar Manualmente
            # solo porque hubo un hueco en un dato que ni siquiera se usó
            # para el número reportado (se sigue confiando en CGM en ambos
            # casos -- esto solo decide la bandera).
            if e_inv_incompleto and isinstance(curva_solarview, pd.Series):
                error_parcial = _error_ventana_solar(curva_cgm, curva_solarview)
                resultado_cgm["error_final_pct"] = error_parcial
                en_rango = _en_rango(error_parcial)
                resultado_cgm["revisar_manualmente"] = not en_rango
                # Si la comparación (aunque con inversores parciales) coincide
                # dentro de rango, es funcionalmente lo mismo que Caso 1 --
                # se confía en CGM sin necesidad de revisión, la única
                # diferencia es que el chequeo fue con datos incompletos en
                # vez del día completo. Se reclasifica a Caso 1 para que
                # "Revisión de hoy" no lo muestre como "Corregido
                # automático" (ámbar) cuando en realidad no se corrigió nada
                # -- 'solenium_completo' sigue registrando que los
                # inversores estuvieron incompletos ese día, independiente
                # del número de Caso (pedido 2026-08-21). Si el error SÍ se
                # sale de rango, se queda en Caso 5 + revisar_manualmente,
                # sin cambios.
                if en_rango:
                    resultado_cgm["caso"] = 1
            return resultado_cgm

        curva = _principal_o_respaldo(curva_ppal, curva_resp)
        if not _tiene_dato(curva):
            # Medidor totalmente caído -- volver a confiar en CGM sería
            # recaer en la misma fuente que esta rama ya decidió no usar. Si
            # hay inversores (aunque el total del día haya quedado parcial),
            # se prefieren (mismo mecanismo de FP que Caso 3). El
            # reconectador es el segundo intento. CGM queda como último
            # recurso si ninguno de los dos tiene nada.
            if e_inv_incompleto:
                fp_val, fp_calc = historial.get_factor_perdida_detalle(frontera_id, fecha)
                if fp_val is not None and isinstance(curva_solarview, pd.Series):
                    e_fp = e_inv_incompleto * fp_val
                    return {
                        "caso": 5, "energia_final_kwh": e_fp,
                        "curva_final": escalar_curva_con_huecos(curva_solarview, e_fp),
                        "fp": fp_val, "fp_calculada": fp_calc,
                        "medidor_usado": "inversores", "revisar_manualmente": True,
                    }
            if id_solarview is not None:
                curva_reconectador = reconectador.get_curva_reconectador(sv, int(id_solarview), fecha_str, capacidad_efectiva_mw)
                if curva_reconectador is not None and curva_reconectador.fillna(0).sum() > 0:
                    return {
                        "caso": 5, "energia_final_kwh": float(curva_reconectador.fillna(0).sum()),
                        "curva_final": curva_reconectador, "medidor_usado": "reconectador",
                        "revisar_manualmente": True,
                        # Ya se consultó arriba -- se reusa como referencia
                        # para no volver a pedirla al final de
                        # clasificar_generacion() (evita una segunda
                        # llamada que podría devolver algo distinto y
                        # contradecir "Fuente usada" en el front).
                        "curva_reconectador_referencia": curva_a_lista(curva_reconectador),
                    }
            return {"caso": 5, "energia_final_kwh": e_cgm, "curva_final": curva_cgm, "medidor_usado": "cgm"}

        sospechoso = frontera_id in MEDIDORES_SIN_INVERSOR_SOSPECHOSOS
        resultado = {
            "caso": 5, "energia_final_kwh": float(curva.fillna(0).sum()), "curva_final": curva,
            "medidor_usado": "principal" if curva is curva_ppal else "respaldo",
        }
        if sospechoso:
            resultado["revisar_manualmente"] = True
        return resultado

    # --- Caso 5 (sin CGM): sin inversores Y sin reporte CGM ese día, pero el
    # medidor sí tiene dato real -- usarlo directo es más preciso que
    # reconstruir de datos crudos (que dependen de la frecuencia de muestreo
    # de la API, ver San Pelayo 2026-08-03: crudos a media resolución dio
    # ~14% menos que el medidor). Solo entra si e_cgm<=0 -- el caso e_cgm>0
    # ya lo maneja el bloque de arriba. Si el medidor TAMBIÉN está caído, no
    # se hace nada acá y sigue cayendo a la cadena de crudos de siempre.
    if e_cgm <= 0:
        # CGM reportó genuinamente 0 (24 horas reales, no ausencia de dato)
        # en un reporte automático válido para hoy -- confiar directo en ese
        # cero, igual que se confiaría en cualquier otro valor de CGM, en
        # vez de caer al medidor de nodo solo porque la suma dio 0
        # (encontrado 2026-09-02 con GD Garza). 'cgm_tiene_dato' distingue
        # esto de "Quoia nunca respondió", que sí debe seguir cayendo al
        # medidor más abajo. No aplica si 'reporte_valido' es False -- un
        # estado administrativo "válido" no garantiza que el canal CGM haya
        # traído algo real detrás.
        if reporte_valido and cgm_tiene_dato:
            return {"caso": 5, "energia_final_kwh": 0.0, "curva_final": curva_cgm, "medidor_usado": "cgm"}

        curva = _principal_o_respaldo(curva_ppal, curva_resp)
        if _tiene_dato(curva):
            # Mismo blindaje que ya tiene el camino de CGM válido más arriba
            # -- si Solenium reportó parcial ese día, se compara el medidor
            # contra Solenium dentro de la ventana solar (ver
            # _error_ventana_solar -- MGS 0025 El Copey Occidente
            # 2026-08-05: medidor 6.861 kWh vs inversores parciales 6.887,4
            # kWh, ~0,3% de diferencia -- no había motivo para marcar
            # Revisar Manualmente a ciegas). Sin inversores con qué
            # comparar, se mantiene el criterio de siempre: queda marcado.
            error_parcial = None
            if e_inv_incompleto and isinstance(curva_solarview, pd.Series):
                error_parcial = _error_ventana_solar(curva, curva_solarview)

                # Si el elegido por defecto (siempre principal, mientras
                # tenga dato) falla la comparación contra inversores pero el
                # respaldo sí pasa, se prefiere el respaldo -- ya no tiene
                # sentido insistir en "siempre principal" si es el respaldo
                # el que de verdad coincide (ver MGS 0026 Valencia Oriente
                # 2026-08-05: principal 4.695,6 kWh vs inversores 7.045,4 kWh,
                # ~33% de diferencia; respaldo 7.054,0 kWh, ~0,1%). Si los dos
                # YA están dentro de rango, se mantiene principal -- no
                # cambia solo porque el otro tenga un error un poco menor.
                if not _en_rango(error_parcial) and curva is curva_ppal and _tiene_dato(curva_resp):
                    error_resp = _error_ventana_solar(curva_resp, curva_solarview)
                    if error_resp is not None and _en_rango(error_resp):
                        curva, error_parcial = curva_resp, error_resp

                # Medidor subreporta contra inversores más de lo tolerado --
                # mismo criterio que Caso 3 (medidores subreportan -> inversores
                # x FP): confiar en el medidor sería reportar un número que ya
                # se sabe está mal, en vez de corregirlo con la fuente que sí
                # cruza (ver MGS Gandalf 2026-08-05: medidor ~20% por debajo de
                # inversores en las horas comunes -- caída puntual a las 11h y
                # degradación progresiva 14h-16h antes de dejar de reportar).
                # Si el medidor SOBREreporta en cambio, se deja igual que
                # siempre (mismo criterio que Caso 4 en otras partes: confiar
                # en el valor más alto, solo marcado para revisar).
                if error_parcial is not None and error_parcial > RANGO_ERROR:
                    fp_val, fp_calc = historial.get_factor_perdida_detalle(frontera_id, fecha)
                    if fp_val is not None:
                        e_fp = e_inv_incompleto * fp_val
                        return {
                            "caso": 5, "energia_final_kwh": e_fp,
                            "curva_final": escalar_curva_con_huecos(curva_solarview, e_fp),
                            "fp": fp_val, "fp_calculada": fp_calc, "error_final_pct": error_parcial,
                            "medidor_usado": "inversores", "revisar_manualmente": True,
                        }

            resultado_medidor = {
                "caso": 5, "energia_final_kwh": float(curva.fillna(0).sum()), "curva_final": curva,
                "medidor_usado": "principal_sin_cgm" if curva is curva_ppal else "respaldo_sin_cgm",
            }
            if error_parcial is not None:
                resultado_medidor["error_final_pct"] = error_parcial
                resultado_medidor["revisar_manualmente"] = not _en_rango(error_parcial)
            else:
                resultado_medidor["revisar_manualmente"] = True
            return resultado_medidor

    # --- Casos 6/7/8: ni CGM ni medidor tienen nada, inversores solo parcial ---
    if e_inv_incompleto:
        # Mismo criterio que ya tiene el Caso 5 sin CGM (medidor caído) unas
        # líneas arriba, y el Caso 8 de datos crudos más abajo: las horas que
        # Solenium SÍ reportó no están "faltando" -- vaciar la curva entera
        # las trataba como si lo estuvieran, y el relleno horario centralizado
        # las volvía a reconstruir con Solenium × FP desde cero (ver MGS 0009
        # Cañahuate 2026-08-05: "revisar" + relleno 8h-23h, cuando en
        # realidad solo faltaban 6h y 7h -- el resto ya era dato real).
        fp_val, fp_calc = historial.get_factor_perdida_detalle(frontera_id, fecha)
        if fp_val is not None and isinstance(curva_solarview, pd.Series):
            e_fp = e_inv_incompleto * fp_val
            return {
                "caso": 3, "energia_final_kwh": e_fp,
                "curva_final": escalar_curva_con_huecos(curva_solarview, e_fp),
                "fp": fp_val, "fp_calculada": fp_calc,
                "medidor_usado": "inversores", "revisar_manualmente": True,
            }
        return {
            "caso": 3, "energia_final_kwh": None, "curva_final": CURVA_VACIA.copy(),
            "fp": None, "fp_calculada": None, "medidor_usado": "revisar", "revisar_manualmente": True,
        }

    # El reconectador se intenta ANTES que datos crudos (no solo como
    # rescate de un aparente apagado) -- validado dos veces contra un valor
    # real (Sabana de Torres 2026-07-25: 25 de 27 días entre 1-3% de CGM
    # real; La Mesa 2026-08-03: 5.431,85 kWh vs medidor 5.434,21 kWh, ~0,04%
    # de diferencia) mientras que crudos puede tener problemas de
    # resolución de muestreo (San Pelayo, ~14% por debajo del medidor) o
    # hasta de escala de unidades (Polaris 1/2, ~1.150x por un nodo que
    # reporta en W en vez de kW). Se marca 'Revisar Manualmente' siempre
    # que se use: un proyecto que llega hasta acá no tiene ninguna otra
    # fuente para confirmar el signo/magnitud del reconectador.
    if id_solarview is not None:
        curva_reconectador = reconectador.get_curva_reconectador(sv, int(id_solarview), fecha_str, capacidad_efectiva_mw)
        if curva_reconectador is not None and curva_reconectador.fillna(0).sum() > 0:
            return {
                "caso": 7, "energia_final_kwh": float(curva_reconectador.fillna(0).sum()),
                "curva_final": curva_reconectador, "medidor_usado": "reconectador",
                "revisar_manualmente": True,
                # Ya se consultó arriba -- se reusa como referencia para no
                # volver a pedirla al final de clasificar_generacion()
                # (evita una segunda llamada que podría devolver algo
                # distinto y contradecir "Fuente usada" en el front -- ver
                # MGS 0033 Sabana de Torres 2026-08-18/21).
                "curva_reconectador_referencia": curva_a_lista(curva_reconectador),
            }

    crudos = datos_crudos.get_datos_crudos(gaia, node_ppal, fecha_str, capacidad_efectiva_mw) if node_ppal else pd.DataFrame()

    if not datos_crudos.proyecto_generando(crudos):
        # Ya se intentó el reconectador arriba -- si llegamos acá es porque
        # no tenía nada (o no hay id_solarview). Último recurso antes de
        # confirmar apagado: sumar todos los inversores de SolarView /power/
        # e integrar. Se reporta DIRECTO, sin FP (no es "inversores vs
        # medidor", es la única lectura de generación disponible).
        if id_solarview is not None:
            resp_power = sv.get_power(int(id_solarview), fecha_str, fecha_str)
            curva_power, _ = solarview_svc.curva_de_power(resp_power, capacidad_efectiva_mw)
            if curva_power.fillna(0).sum() > 0:
                return {
                    "caso": 7, "energia_final_kwh": float(curva_power.fillna(0).sum()),
                    "curva_final": curva_power, "medidor_usado": "solenium_power",
                    "revisar_manualmente": True,
                }
        # 0 kWh acá es la ausencia de CUALQUIER fuente (CGM, medidor,
        # inversores, reconectador, Solenium power) -- no una confirmación
        # real de que el proyecto está apagado. Un proyecto puede seguir
        # generando y que las 5 fuentes fallen el mismo día (real: San Diego
        # Sur 2026-08-03, sin medidor propio, solo vive de CGM -- si CGM no
        # responde ese día, no queda ninguna otra fuente). Marcar para
        # revisar en vez de reportar 0 con la misma confianza que un
        # apagado real y confirmado.
        return {
            "caso": 6, "energia_final_kwh": 0.0, "curva_final": CURVA_CERO.copy(),
            "medidor_usado": "ninguno", "revisar_manualmente": True,
        }

    if datos_crudos.datos_completos(crudos):
        total_riemann = datos_crudos.riemann_eae(crudos)
        return {
            "caso": 7, "energia_final_kwh": total_riemann,
            "curva_final": datos_crudos.curva_horaria_ap(crudos), "medidor_usado": "crudos",
        }

    # Caso 8: datos crudos incompletos -- se aprovechan las horas que sí
    # tienen cobertura real; el relleno centralizado (reconectador ->
    # Solenium × FP -> histórico) se encarga de completar lo que falte.
    curva_parcial = datos_crudos.curva_horaria_ap_con_huecos(crudos)
    return {
        "caso": 8, "energia_final_kwh": float(curva_parcial.fillna(0).sum()),
        "curva_final": curva_parcial, "medidor_usado": "crudos_parcial",
    }


def clasificar_generacion(
    gaia: GaiaClient,
    sv: SolarViewClient,
    frontera_id: int,
    frt_code: str,
    border_meta: dict | None,
    project_id_solarview: int | None,
    mapa_medidor_nodo: dict[int, int],
    fecha: date,
    capacidad_efectiva_mw: float | None = None,
) -> dict:
    """Clasifica una frontera de Generación para un día. Punto de entrada
    público -- puerto del bloque centralizado de clasificar() (repo
    Reporte-Energia), fusionado con _clasificar_fila() en una sola pasada
    por frontera.

    Retorna un dict listo para volcar en ReporteEnergiaGeneracion (columnas
    en snake_case, 'curva_final' como pd.Series[0..23]).
    """
    fecha_str = str(fecha)

    if frontera_id in FRONTERAS_TERCEROS:
        return {
            "caso": 0, "energia_final_kwh": None, "curva_final": CURVA_VACIA.copy(),
            "revisar_manualmente": True, "medidor_usado": "externo",
            "energia_cgm_kwh": None, "estado_reporte": None,
            "energia_solenium_kwh": None, "solenium_completo": None, "nota_solenium": None,
            "energia_medidor_principal_kwh": None, "energia_medidor_respaldo_kwh": None,
            "medidor_principal_completo": None, "medidor_respaldo_completo": None,
            "horas_rellenadas_reconectador": None, "horas_rellenadas_solenium": None,
            "horas_rellenadas_historico": None, "recuperacion_datos": None,
        }

    # --- CGM ---
    border_id = border_meta.get("border_id") if border_meta else None
    reporte = gaia.get_border_report_status(int(border_id), fecha_str) if border_id else None
    reporte_valido = bool(reporte) and str(reporte.get("status", "")).upper() in ESTADOS_AUTOMATICO
    estado_reporte = str(reporte.get("status")).upper() if reporte else None
    # 'cgm_tiene_dato' distingue "Quoia trajo 24 horas reales (aunque sumen 0)"
    # de "no hay reported_data_main en absoluto" -- ambos casos colapsan al
    # mismo curva_cgm (CURVA_CERO) más abajo, así que hay que guardar la
    # distinción ANTES de ese colapso para que _decidir_caso() pueda confiar
    # en un CGM que reportó genuinamente 0 en vez de caer al medidor solo
    # porque la suma dio 0 (ver Caso 5 sin CGM, encontrado 2026-09-02 con GD
    # Garza -- mismo fix portado de Reporte-Energia::clasificador.py).
    cgm_tiene_dato = bool(reporte and reporte.get("reported_data_main"))
    if cgm_tiene_dato:
        curva_cgm = pd.Series(reporte["reported_data_main"][:24], index=HORAS, dtype=float)
        # Mismo criterio que medidor/SolarView/reconectador (ver
        # limite_plausible_kwh() en utils.py) -- el reporte oficial de
        # Quoia al ASIC tampoco es inmune a un glitch de telemetría del
        # lado del medidor que lo alimenta. A diferencia de esas otras
        # fuentes no se enmascara solo la hora implausible: Caso 1 usa
        # curva_cgm DIRECTO como curva_final y no está en
        # CASOS_CON_RELLENO_HORARIO, así que un hueco ahí quedaría sin
        # rellenar y sin marcar revisar_manualmente. Se descarta CGM
        # entero para esta fila -- reporte_valido=False fuerza la caída a
        # la cadena de medidor/inversores (Casos 2/3/4), que sí maneja
        # huecos correctamente.
        limite = limite_plausible_kwh(capacidad_efectiva_mw)
        if limite is not None and (curva_cgm.abs() > limite).any():
            curva_cgm = CURVA_CERO.copy()
            reporte_valido = False
            cgm_tiene_dato = False  # descartado por implausible, no es un cero confiable
    else:
        curva_cgm = CURVA_CERO.copy()
    e_cgm = float(curva_cgm.fillna(0).sum())

    # --- SolarView (inversores) -- ANTES del medidor a propósito, ver abajo ---
    curva_solarview, solarview_completo = solarview_svc.curva_generacion(
        sv, project_id_solarview, fecha_str, capacidad_efectiva_mw,
    )
    e_inv_original = float(curva_solarview.fillna(0).sum())

    # Un hueco de telemetría en SolarView hace que e_inv_original sea un total
    # parcial -- de acá en adelante SolarView ya no es confiable para validar
    # cruzado, se trata como si no hubiera inversores en absoluto (e_inv=0)
    # para caer en la misma cadena de respaldo (CGM -> medidor -> datos
    # crudos). El total parcial se guarda aparte (e_inv_incompleto) por si
    # no hay ninguna otra fuente más -- ver Caso 5 y Casos 6/7/8 en
    # _decidir_caso.
    e_inv = e_inv_original
    e_inv_incompleto = None
    if e_inv > 0 and not solarview_completo:
        e_inv_incompleto = e_inv
        e_inv = 0.0

    # --- Medidor de nodo ---
    # Mismo chequeo de Caso 1 que hace _decidir_caso() más abajo, pero ANTES
    # de traer el medidor -- Caso 1 no usa el medidor para nada (valida CGM
    # contra inversores, no contra el medidor). Si ya se sabe que hoy va a
    # ganar Caso 1, no tiene sentido pagar recuperación activa (hasta 90s
    # interrogando el dispositivo) sobre un medidor que ni se va a usar para
    # decidir -- pero SÍ se sigue trayendo la lectura PASIVA (recuperar=False),
    # porque el histórico de Factor de Pérdida necesita medidor de TODOS los
    # días con dato, sin importar qué Caso ganó (ver historial.py). Motivado
    # por Bongos/Paso Norte: la recuperación activa a las 3:30am no siempre
    # alcanza a estabilizar el dato de todas formas (ver conversación de
    # sesión), así que gastarla en fronteras que ni la necesitan es puro
    # costo sin beneficio.
    es_caso1_seguro = (
        reporte_valido and e_inv > 0 and e_cgm > 0
        and _en_rango(_error_con_curva(e_inv, curva_cgm))
    )
    # Mediana histórica -- solo como referencia de plausibilidad para la
    # lectura del medidor (ver mediana_referencia en curvas_de_frontera), no
    # se usa el FP acá. Solo hace falta pedirla cuando SÍ se va a intentar
    # recuperación -- si es_caso1_seguro, ni siquiera se consulta.
    mediana_hist = None
    if not es_caso1_seguro:
        mediana_hist, _ = historial.get_mediana_generacion(frontera_id, fecha)
    main_meter = border_meta.get("main_meter") if border_meta else None
    backup_meter = border_meta.get("backup_meter") if border_meta else None
    c = curvas.curvas_de_frontera(
        gaia, mapa_medidor_nodo, main_meter, backup_meter, fecha_str, frt_code,
        recuperar=not es_caso1_seguro, mediana_referencia=mediana_hist,
        capacidad_efectiva_mw=capacidad_efectiva_mw,
    )
    curva_ppal, curva_resp = c["curva_ppal"], c["curva_resp"]
    completo_ppal, completo_resp = c["ppal_completo"], c["resp_completo"]

    resultado = _decidir_caso(
        frontera_id, fecha, fecha_str,
        e_cgm, curva_cgm, reporte_valido, cgm_tiene_dato,
        curva_ppal, curva_resp, completo_ppal, completo_resp,
        e_inv, e_inv_incompleto, curva_solarview,
        project_id_solarview, c["node_ppal"], gaia, sv,
        capacidad_efectiva_mw=capacidad_efectiva_mw,
    )
    revisar = resultado.get("revisar_manualmente", False)

    # Un hueco de telemetría en SolarView marca revisión manual (no hay forma
    # de recuperarlo como con los medidores) -- excepto cuando el resultado
    # ya viene de un camino de Caso 5 que YA comparó contra el total parcial
    # de inversores (_error_ventana_solar) y decidió su propia bandera con
    # ese chequeo -- no la pise acá pisándola con "no era 'cgm'" (ver MGS
    # 0014 El Olimpo 2026-08-10: 'principal_sin_cgm' con 0,03% de diferencia
    # -- pasaba su propio chequeo pero esta regla lo volvía a marcar solo
    # por no ser exactamente 'cgm').
    if (
        resultado.get("medidor_usado") not in ("cgm", "principal_sin_cgm", "respaldo_sin_cgm")
        and e_inv_original > 0 and not solarview_completo
    ):
        revisar = True

    # --- Relleno horario centralizado ---
    # Solo el cero directo (certeza física, fuera de la ventana solar) se
    # aplica automático acá. reconectador/Solenium × FP/histórico dejaron de
    # rellenar la curva final solos (decisión explícita 2026-08-12: mezclar
    # otra fuente en la curva final sin que nadie lo pidiera era demasiado
    # invasivo) -- quedan disponibles como acción manual desde el front, ver
    # POST /fronteras/{id}/rellenar-horario en reporte_energia.py, que
    # reusa reconectador.rellenar_horas_faltantes() sobre lo que haya
    # quedado acá.
    curva_actual = resultado.get("curva_final")
    horas_reconectador, horas_solarview_h, horas_historico = set(), set(), set()
    horas_fuera_ventana_directo: set[int] = set()
    if (
        resultado["caso"] in CASOS_CON_RELLENO_HORARIO
        and isinstance(curva_actual, pd.Series)
        and curva_actual.isna().any()
    ):
        huecos_iniciales = curva_actual[curva_actual.isna()].index
        horas_fuera_ventana_directo = {h for h in huecos_iniciales if h not in HORAS_SOLARES}
        if horas_fuera_ventana_directo:
            curva_actual = curva_actual.copy()
            curva_actual[sorted(horas_fuera_ventana_directo)] = 0.0
            resultado["curva_final"] = curva_actual
            resultado["energia_final_kwh"] = float(curva_actual.fillna(0).sum())

        # Un hueco DENTRO de la ventana solar sin dato marca revisar -- ahí
        # la certeza física no ayuda (podría ser generación real) y, sin el
        # relleno automático de las otras tres fuentes, no queda nada más
        # con qué completarlo desde acá salvo la acción manual.
        if any(h in HORAS_SOLARES for h in curva_actual[curva_actual.isna()].index):
            revisar = True

        # medidor_usado='revisar' es el valor "no se pudo construir nada"
        # que puso _decidir_caso() para una curva vacía -- si el cero directo
        # SÍ logró llenar horas (aunque no todas), ya no es cierto que no
        # haya fuente (ver Granja Solar Uruaco 2026-08-03: caso 3,
        # medidor_usado seguía en 'revisar' con energía real reconstruida).
        if resultado.get("medidor_usado") == "revisar" and horas_fuera_ventana_directo:
            resultado["medidor_usado"] = "relleno_horario"

    resultado["revisar_manualmente"] = revisar
    resultado["horas_rellenadas_reconectador"] = sorted(horas_reconectador) or None
    resultado["horas_rellenadas_solenium"] = sorted(horas_solarview_h) or None
    resultado["horas_rellenadas_historico"] = sorted(horas_historico) or None
    resultado["energia_cgm_kwh"] = e_cgm
    resultado["estado_reporte"] = estado_reporte
    resultado["energia_solenium_kwh"] = e_inv_original
    resultado["solenium_completo"] = bool(solarview_completo)
    resultado["nota_solenium"] = "Sin registro en API SolarView" if project_id_solarview is None else None
    resultado["energia_medidor_principal_kwh"] = float(curva_ppal.fillna(0).sum())
    resultado["energia_medidor_respaldo_kwh"] = float(curva_resp.fillna(0).sum())
    resultado["medidor_principal_completo"] = bool(completo_ppal)
    resultado["medidor_respaldo_completo"] = bool(completo_resp)
    resultado["recuperacion_datos"] = c.get("recuperacion_datos")
    # Curvas de referencia (medidor/SolarView) tal como estaban AL MOMENTO de
    # clasificar -- antes solo se guardaba el total (energia_medidor_..._kwh),
    # la curva completa se volvía a pedir en vivo cada vez que se abría el
    # detalle, lo que podía mostrar un valor distinto al que realmente se usó
    # si Quoia corrige un dato después (ver MGS 0032 El Paso Norte
    # 2026-08-05: medidor doblado por un glitch de Quoia al momento de
    # clasificar, ya autocorregido para cuando se revisó -- "Fuente usada"
    # y "Detalle de las fuentes" mostraban números distintos sin explicación).
    resultado["curva_medidor_principal"] = curva_a_lista(curva_ppal)
    resultado["curva_medidor_respaldo"] = curva_a_lista(curva_resp)
    resultado["curva_solenium_referencia"] = (
        curva_a_lista(curva_solarview) if isinstance(curva_solarview, pd.Series) else None
    )
    # Reconectador de referencia -- mismo trato que medidor/Solenium arriba:
    # se consulta SIEMPRE en la clasificación diaria y se guarda, para que
    # "Detalle de las fuentes" lo muestre como una fuente más sin tener que
    # volver a pedirlo en vivo cada vez que se abre el panel (pedido
    # 2026-08-21: "no quiero que persista el criterio de rellenar horas").
    # Si Caso 5/7 ya la consultó (medidor_usado == "reconectador", el
    # reconectador fue la fuente COMPLETA del día) no se vuelve a pedir --
    # una segunda llamada podía devolver algo distinto y contradecir
    # "Fuente usada" (ver MGS 0033 Sabana de Torres 2026-08-18/21: decía
    # "Fuente usada: Reconectador" pero "Detalle de las fuentes" mostraba
    # "Sin dato"). None si el proyecto no tiene reconectador instalado o la
    # consulta falla -- get_curva_reconectador() ya maneja eso.
    if "curva_reconectador_referencia" not in resultado:
        curva_reconectador_ref = (
            reconectador.get_curva_reconectador(sv, project_id_solarview, fecha_str, capacidad_efectiva_mw)
            if project_id_solarview is not None else None
        )
        resultado["curva_reconectador_referencia"] = (
            curva_a_lista(curva_reconectador_ref) if curva_reconectador_ref is not None else None
        )
    # Las horas del CGM, tal como se leyeron arriba -- una sola línea porque la
    # curva ya está en la mano (no cuesta una llamada más). Se guarda SIEMPRE,
    # gane o no el CGM: cuando gana, `curva_final` ya la tiene, pero cuando
    # pierde era el único lado donde no quedaba registro, y es justo el caso en
    # que alguien quiere poder adoptarla a mano. Así el panel la lee de la base
    # en vez de volver a preguntarle a Quoia en cada apertura -- mismo criterio
    # que las de medidor/Solenium/reconectador.
    resultado.setdefault("curva_cgm_referencia", curva_a_lista(curva_cgm))
    resultado.setdefault("fp", None)
    resultado.setdefault("fp_calculada", None)
    resultado.setdefault("error_final_pct", None)
    return resultado
