"""Las correcciones manuales del reporte de energía.

Puerto de las escrituras de `app/api/v1/reporte_energia.py`.

**Adoptar el medidor principal NO apaga en silencio el aviso del respaldo.**
`_curva_respaldo_en_vivo` trae la curva del respaldo solo para el chequeo de
coherencia y NO la persiste: una actualización silenciosa del snapshot al
confirmar Principal apagaba el aviso "el medidor muestra un valor distinto en
Quoia" sin que nadie hubiera revisado ESE medidor (pedido del 2026-08-26).

**Cuando la fuente confirmada no es un medidor, el respaldo se limpia.** Se
quedaba con el snapshot de la clasificación ANTERIOR, calculado contra el
`curva_final` viejo — el detalle, el envío y el Excel solo recalculan cuando el
campo es None, así que ese respaldo desactualizado se mostraba y se enviaba tal
cual (Chiriguaná Norte 2 y Verso: respaldo ~2× el nuevo principal).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError

from api.exceptions import NoProcesable, ServicioNoDisponible
from apps.energia.models import (
    ReporteEnergiaConsumo, ReporteEnergiaExclusion, ReporteEnergiaGeneracion,
)
from apps.energia.services.reporte import (
    curvas, excel_terceros, historial, reconectador, recuperacion,
)
from apps.energia.services.reporte.clasificador_consumo import (
    rellenar_horas_faltantes_consumo,
)
from apps.energia.services.reporte.clasificador import FRONTERAS_TERCEROS
from apps.energia.services.reporte.utils import (
    actualizar_respaldo_final, curva_a_lista, escalar_curva, lista_a_curva,
    rellenar_con_otro_medidor,
)
from apps.energia.services.reporte.vistas import (
    _construir_detalle, _fila_por_id, _nombre_frontera,
)
from apps.fronteras.models import Frontera

# `ponytail: el cliente de Quoia sigue en app/services/mgs/`.
from app.services.mgs.gaia_client import GaiaClient
from app.services.mgs.solarview_client import SolarViewClient


class _SinFilas(Exception):
    """El Excel se leyó pero no traía ninguna fila 'Primary' válida.

    Sirve para revertir la transacción de la carga sin confundirse con un
    `ValueError` de formato, que significa otra cosa.
    """


def _curva_respaldo_en_vivo(front: Frontera, fecha: date, es_generacion: bool) -> list | None:
    """Trae en vivo la curva del medidor de RESPALDO -- únicamente para
    alimentar el chequeo de coherencia de curva_respaldo_a_reportar()
    cuando se confirma 'Medidor principal' como fuente (ver editar_curva).
    Generación usa 'eae', Consumo 'iae' (extendido a Consumo 2026-08-26).

    A propósito NO se persiste en curva_medidor_respaldo: eso seguiría
    disparando el aviso "el medidor ya muestra un valor distinto en
    Quoia" hasta que la persona revise y confirme ESE medidor
    explícitamente -- una actualización silenciosa del snapshot al
    adoptar Principal apagaba el aviso de Respaldo sin que nadie lo
    hubiera revisado (pedido de Sara 2026-08-26, tras ver que el aviso ya
    no aparecía en 'Detalle de las fuentes' después de guardar).

    Best-effort: None si Quoia falla o no hay match -- quien llama debe
    seguir sin este dato (curva_respaldo_a_reportar cae a estimado)."""
    try:
        gaia = GaiaClient()
        mapa_nodo = curvas.construir_mapa_medidor_nodo(gaia)
        borders = curvas.construir_mapa_borders(gaia)
        meta = borders.get((front.codigo_frontera or "").strip().lower())
        if not meta:
            return None
        # Solo Generación -- Consumo no tiene un concepto de capacidad
        # efectiva definido todavía (mismo criterio que drift_medidores.py).
        capacidad_efectiva_mw = (
            float(front.proyecto.potencia_instalada_kwp) / 1000
            if es_generacion and front.proyecto_id and front.proyecto.potencia_instalada_kwp is not None else None
        )
        var_name = "eae" if es_generacion else "iae"
        _, curva_r = curvas.curva_medidor_en_vivo(
            gaia, mapa_nodo, meta.get("main_meter"), meta.get("backup_meter"),
            str(fecha), front.codigo_frontera, var_name, capacidad_efectiva_mw,
        )
        return curva_a_lista(curva_r)
    except Exception:
        return None


def _revisar_respaldo_en_vivo(front: Frontera, rep, fecha: date, es_generacion: bool) -> None:
    """Si curva_final ya viene del medidor PRINCIPAL o de CGM ya validado
    (Caso 1/'CGM' -- ampliado 2026-08-26, ver curva_respaldo_a_reportar),
    vuelve a evaluar el respaldo en vivo contra la tolerancia de
    coherencia y adopta el nuevo snapshot SOLO si pasa -- llamada por
    editar_curva() (al confirmar Principal), recuperar_medidor() y
    revisar_respaldo() (acciones explícitas de revisar el respaldo, ver
    MGS Agustín 1 2026-08-26: Principal ya correcto y automático -- nunca
    pasa por editar_curva() -- pero el respaldo cambió en Quoia y no
    había forma de que el sistema lo reevaluara). Mismo criterio en los
    tres lugares: si no pasa, el snapshot no se toca y el aviso de cambio
    sigue visible.

    Generación y Consumo (extendido 2026-08-26) -- var_name/capacidad
    efectiva se resuelven según es_generacion dentro de
    _curva_respaldo_en_vivo()."""
    mu = rep.medidor_usado or ""
    if not (mu.startswith("principal") or mu == "cgm"):
        return
    curva_resp_viva = _curva_respaldo_en_vivo(front, fecha, es_generacion)
    actualizar_respaldo_final(rep, curva_resp_viva)
    if curva_resp_viva is not None and rep.respaldo_final_origen == "medidor":
        rep.curva_medidor_respaldo = curva_resp_viva


def editar_curva(frontera_id: int, fecha: date, datos: dict) -> dict:
    """Guarda la curva corregida a mano y, con ella, la fuente que la explica.

    **Los flags de "hora rellenada" se limpian.** Eran de la curva ANTERIOR: si
    la persona reemplaza `curva_final`, esas horas ya no vienen de ese relleno, y
    dejarlos mostraba el diamante de "Rellenado" sobre datos que ya no lo son.

    **Confirmar un medidor cambia `caso`, no solo `medidor_usado`.** El historial
    filtra por `caso`, así que una corrección manual con dato real nunca contaba
    para la mediana de días futuros mientras `caso` siguiera en lo que decidió el
    clasificador. 'Inversores × FP', 'Histórico propio' y 'Matriz de ceros' NO lo
    tocan: son estimaciones, no lectura real.
    """
    front, rep, Modelo = _fila_por_id(frontera_id, fecha)
    es_generacion = Modelo is ReporteEnergiaGeneracion
    curva_final = datos["curva_final"]
    if len(curva_final) != 24:
        raise NoProcesable("curva_final debe tener 24 valores")

    curva = lista_a_curva(curva_final)
    rep.curva_final = curva_a_lista(curva)
    rep.energia_final_kwh = float(curva.fillna(0).sum())
    rep.editado_manualmente = True
    # Los flags de 'hora rellenada' (reconectador/Solenium/histórico) eran
    # de la curva ANTERIOR -- si la persona reemplaza curva_final a mano
    # (otra fuente, o celda por celda), esas horas ya no vienen de ese
    # relleno; dejarlos quedaba mostrando el diamante dorado de 'Rellenado'
    # sobre datos que ya no lo son (ver GD Naos 1 2026-08-12: 'Medidor
    # principal' elegido a mano, pero seguía marcando 14h-16h como
    # 'Rellenado (histórico)', dato del clasificador automático original).
    if Modelo is ReporteEnergiaGeneracion:
        rep.horas_rellenadas_reconectador = None
        rep.horas_rellenadas_solenium = None
    rep.horas_rellenadas_historico = None
    # 'Fuente usada' quedaba mostrando lo que el clasificador decidió
    # originalmente (ej. 'Histórico propio') aunque la persona ya hubiera
    # reemplazado la curva con otra fuente ('Reportar con otra fuente') --
    # cualquier guardado manual reemplaza la fuente reportada. Si el editor
    # se llenó desde una de esas opciones, se refleja esa fuente específica
    # (mismos valores que ya usa ETIQUETAS_FUENTE en el front: 'Medidor
    # principal'/'respaldo', 'Inversores × FP', 'Histórico propio'); si fue
    # edición celda por celda sin pasar por ahí, o si se usó 'Matriz de
    # ceros' (no es una fuente real, solo un valor de reemplazo), queda el
    # genérico "Editado manualmente".
    FUENTES_MANUALES_VALIDAS = {"principal", "respaldo", "inversores", "historico", "reconectador"}
    fuente = datos.get("fuente")
    rep.medidor_usado = fuente if fuente in FUENTES_MANUALES_VALIDAS else "editado_manualmente"
    # Si la fuente elegida es un medidor, lo que se acaba de guardar pasa a
    # ser el nuevo snapshot de ESE medidor -- si no se actualiza, 'Detalle de
    # las fuentes' y el aviso 'el medidor muestra un valor distinto en Quoia'
    # seguian comparando contra el numero congelado de la clasificacion
    # original, aunque la persona ya hubiera adoptado la opcion '(actualizado)'
    # de 'Reportar con otra fuente' (esa opcion cambia curva_final pero nunca
    # tocaba curva_medidor_principal/respaldo, ver captura 2026-08-20).
    if fuente == "principal":
        rep.curva_medidor_principal = rep.curva_final
    elif fuente == "respaldo":
        rep.curva_medidor_respaldo = rep.curva_final
    # Si la persona confirma que el MEDIDOR (no una estimación) es la
    # fuente correcta, 'caso' se actualiza para que esta fila SÍ pueda
    # alimentar la mediana/forma histórica de días futuros -- antes quedaba
    # congelado en lo que decidió el clasificador automático (ej. caso
    # 'Histórico' o 3), y CASOS_CONFIABLES_GENERACION/CONSUMO en
    # historial.py filtran por ese campo, no por medidor_usado, así que una
    # corrección manual con dato real nunca contaba (ver Valencia Oriente 2
    # Consumo 2026-08-12: editada a 'Medidor principal' y validada, pero
    # 'caso' seguía en 'Histórico'). 'Inversores × FP', 'Histórico propio' y
    # 'Matriz de ceros' siguen sin tocar 'caso' -- son estimaciones o un
    # valor de reemplazo, no una lectura real del medidor (mismo criterio
    # que ya excluye el Caso 3 -- Inversores × FP automático -- de
    # CASOS_CONFIABLES_GENERACION).
    #
    # El RECONECTADOR entra acá por la misma razón que los medidores, no por
    # excepción: es la lectura de un dispositivo físico, no una estimación, y
    # el clasificador que lo elige solo ya guarda Caso 5 o 7 -- los dos
    # confiables. Sin esto, confirmarlo a mano valía MENOS que dejarlo
    # automático: el 'caso' se quedaba en el de la rama anterior (ej. 3,
    # inversores parciales × FP, que no es confiable) y ese día no alimentaba
    # la mediana, aunque el número reportado fuera el mismo dato del mismo
    # dispositivo (Cumbia Generación 2026-09-09). Solo Generación lo alcanza:
    # el reconectador es de /relay/ de Solenium y Consumo no tiene.
    if fuente in ("principal", "respaldo", "reconectador"):
        rep.caso = "Medidor" if Modelo is ReporteEnergiaConsumo else 5
    # curva_final/medidor_usado (y, para 'principal', curva_medidor_principal)
    # ya quedaron fijados arriba con lo que la persona acaba de confirmar --
    # recalcular acá lo que se va a reportar como Backup, para que quede
    # congelado con la corrección y no con el resultado del clasificador
    # automático original (mismo motivo que curva_medidor_principal arriba).
    # Si la persona llenó la columna de Respaldo a mano en la tabla de
    # corrección, eso manda tal cual (origen 'manual') -- no se recalcula
    # con curva_respaldo_a_reportar().
    curva_respaldo = datos.get("curva_respaldo_final")
    if curva_respaldo is not None:
        if len(curva_respaldo) != 24:
            raise NoProcesable("curva_respaldo_final debe tener 24 valores")
        curva_resp = lista_a_curva(curva_respaldo)
        rep.curva_respaldo_final = curva_a_lista(curva_resp)
        rep.respaldo_final_origen = "manual"
    else:
        # Si se confirma Principal (o CGM), revisa el respaldo en vivo
        # contra la tolerancia y adopta su snapshot SOLO si pasa -- ver
        # _revisar_respaldo_en_vivo(). Generación y Consumo.
        #
        # Para cualquier OTRA fuente (reconectador, inversores, histórico,
        # edición celda por celda) _revisar_respaldo_en_vivo() no hace
        # nada -- así que curva_respaldo_final se quedaba con el snapshot
        # de la clasificación ANTERIOR, calculado contra el curva_final
        # VIEJO. _construir_detalle()/_enviar_a_quoia()/el Excel solo
        # recalculan cuando el campo es None (mismo criterio en los tres),
        # así que ese respaldo desactualizado se seguía mostrando y
        # enviando tal cual, sin relación con el Principal recién guardado
        # (bug real: Chiriguaná Norte 2 y Verso, respaldo ~2x el nuevo
        # Principal en vez de ±1%). Se limpia para que los tres lo
        # recalculen al vuelo contra la curva que se acaba de guardar.
        mu = rep.medidor_usado or ""
        if mu.startswith("principal") or mu == "cgm":
            _revisar_respaldo_en_vivo(front, rep, fecha, es_generacion)
        else:
            rep.curva_respaldo_final = None
            rep.respaldo_final_origen = None
    # `revisar_manualmente` NO se toca acá: la corrección queda pendiente de
    # un "Validar" explícito.
    rep.save()
    return _construir_detalle(frontera_id, fecha)


def rellenar_horario(frontera_id: int, fecha: date) -> dict:
    """Rellena a mano las horas que quedaron sin dato en 'curva_final' --
    acción explícita, ya NO pasa sola durante la clasificación automática
    (decisión 2026-08-12: mezclar otra fuente en la curva final sin que
    nadie lo pidiera era demasiado invasivo). Orden de fuentes, la primera
    que tenga dato para cada hora gana:

    1. El OTRO medidor (el que no ganó como medidor_usado) -- mismo
       consumo/generación física, dato real, no una estimación.
    2. Generación: reconectador, luego SolarView × FP.
    3. Histórico propio (mediana × forma) -- último recurso en ambos árboles.

    Aplica a Generación y Consumo. No fuerza revisar_manualmente -- es una
    acción manual y consciente, igual que editar_curva(); queda pendiente
    de un "Validar Frontera" explícito.
    """
    front, rep, Modelo = _fila_por_id(frontera_id, fecha)
    es_generacion = Modelo is ReporteEnergiaGeneracion

    curva_actual = lista_a_curva(rep.curva_final)
    if not curva_actual.isna().any():
        raise ValidationError("Esta curva no tiene horas sin dato para rellenar")

    curva_actual, horas_medidor_cruzado = rellenar_con_otro_medidor(
        curva_actual, rep.medidor_usado, rep.curva_medidor_principal, rep.curva_medidor_respaldo,
    )

    horas_reconectador, horas_solenium_h, horas_historico = set(), set(), set()
    curva_reconectador_ref = None
    fp = fp_calc = None
    if curva_actual.isna().any():
        if es_generacion:
            proyecto = front.proyecto if front.proyecto_id else None
            project_id_solarview = (
                int(proyecto.project_id_solarview)
                if front.proyecto_id and proyecto.project_id_solarview
                and str(proyecto.project_id_solarview).isdigit()
                else None
            )
            curva_solarview = lista_a_curva(rep.curva_solenium_referencia) if rep.curva_solenium_referencia else None
            if rep.fp is not None:
                fp, fp_calc = float(rep.fp), float(rep.fp_calculada) if rep.fp_calculada is not None else None
            else:
                fp, fp_calc = historial.get_factor_perdida_detalle(frontera_id, fecha)

            sv = SolarViewClient()
            # Si la clasificación diaria ya consultó y guardó el
            # reconectador (ver clasificar_generacion), se reusa en vez de
            # volver a pedirlo a SolarView -- evita una llamada duplicada
            # cuando "Rellenar horas" se usa el mismo día (2026-08-21).
            curva_reconectador_conocida = (
                lista_a_curva(rep.curva_reconectador_referencia)
                if rep.curva_reconectador_referencia is not None else None
            )
            curva_actual, horas_reconectador, horas_solenium_h, horas_historico, curva_reconectador_ref = (
                reconectador.rellenar_horas_faltantes(
                    sv, curva_actual, project_id_solarview, str(fecha),
                    frontera_id=frontera_id, curva_solarview=curva_solarview, fp=fp,
                    curva_reconectador_conocida=curva_reconectador_conocida,
                    capacidad_efectiva_mw=(
                        float(proyecto.potencia_instalada_kwp) / 1000
                        if proyecto and proyecto.potencia_instalada_kwp is not None else None
                    ),
                )
            )
        else:
            curva_actual, horas_historico = rellenar_horas_faltantes_consumo(
                curva_actual, frontera_id, fecha)

    # La curva de referencia del reconectador se guarda en cuanto se
    # consultó y respondió algo -- independiente de si alguna de sus horas
    # terminó usándose para rellenar 'curva_final'. Antes solo se guardaba
    # si el relleno completo tenía éxito (ver el 400 de abajo), así que un
    # reconectador que respondió pero cuyas horas ya estaban cubiertas por
    # otra fuente (o fuera de HORAS_RECONECTADOR) nunca se guardaba ni se
    # veía en el chart aunque el dato existiera (pedido 2026-08-21).
    if es_generacion and curva_reconectador_ref is not None:
        rep.curva_reconectador_referencia = curva_a_lista(curva_reconectador_ref)
        rep.save(update_fields=["curva_reconectador_referencia"])

    if not (horas_medidor_cruzado or horas_reconectador or horas_solenium_h or horas_historico):
        raise ValidationError("Ninguna fuente tenía dato para las horas faltantes")

    rep.curva_final = curva_a_lista(curva_actual)
    rep.energia_final_kwh = float(curva_actual.fillna(0).sum())
    rep.horas_rellenadas_medidor_cruzado = sorted(horas_medidor_cruzado) or None
    rep.horas_rellenadas_historico = sorted(horas_historico) or None
    if es_generacion:
        rep.horas_rellenadas_reconectador = sorted(horas_reconectador) or None
        rep.horas_rellenadas_solenium = sorted(horas_solenium_h) or None
        if curva_reconectador_ref is not None:
            rep.curva_reconectador_referencia = curva_a_lista(curva_reconectador_ref)
        if horas_solenium_h and rep.fp is None:
            rep.fp = fp
            rep.fp_calculada = fp_calc
        if rep.medidor_usado == "revisar":
            rep.medidor_usado = "relleno_horario"
    # revisar_manualmente NO se fuerza acá -- ese forzado tenía sentido
    # mientras el relleno era automático y silencioso (nadie lo notaba sin
    # la bandera); ahora que es una acción manual y consciente (la persona
    # ve las fuentes disponibles y decide hacer clic), queda igual que
    # editar_curva(): pendiente de un "Validar Frontera" explícito, sin que
    # el botón se lo imponga.
    # Evita que una re-ejecución del clasificador para este mismo día pise
    # este relleno manual (mismo guard que ya protege 'Reportar con otra
    # fuente', ver editar_curva() arriba).
    rep.editado_manualmente = True
    rep.save()
    return _construir_detalle(frontera_id, fecha)


def deshacer_relleno(frontera_id: int, fecha: date) -> dict:
    """Revierte lo que puso 'Rellenar horas' -- vuelve a NaN exactamente las
    horas que quedaron marcadas en horas_rellenadas_* (medidor_cruzado/
    reconectador/solenium/historico), sin tocar ninguna otra hora de la
    curva. Si medidor_usado había pasado a 'relleno_horario' (venía de
    'revisar'), se restaura a 'revisar' -- en cualquier otro caso
    medidor_usado no lo había tocado el relleno, así que tampoco se toca acá.
    reconectador/Solenium son campos exclusivos de Generación (Consumo no
    tiene esas columnas) -- guardado con es_generacion, igual que el resto
    de este archivo.
    """
    front, rep, Modelo = _fila_por_id(frontera_id, fecha)
    es_generacion = Modelo is ReporteEnergiaGeneracion

    horas_reconectador = (rep.horas_rellenadas_reconectador or []) if es_generacion else []
    horas_solenium = (rep.horas_rellenadas_solenium or []) if es_generacion else []
    horas_a_revertir = set(
        (rep.horas_rellenadas_medidor_cruzado or [])
        + horas_reconectador
        + horas_solenium
        + (rep.horas_rellenadas_historico or [])
    )
    if not horas_a_revertir:
        raise ValidationError("Esta frontera no tiene un relleno horario para deshacer")

    curva = lista_a_curva(rep.curva_final)
    for h in horas_a_revertir:
        curva[h] = None
    rep.curva_final = curva_a_lista(curva)
    rep.energia_final_kwh = float(curva.fillna(0).sum())
    rep.horas_rellenadas_medidor_cruzado = None
    rep.horas_rellenadas_historico = None
    if es_generacion:
        rep.horas_rellenadas_reconectador = None
        rep.horas_rellenadas_solenium = None
        # Curva de referencia que "Rellenar horas" guarda solo para mostrar
        # en el gráfico (ReporteEnergiaCurvaChart.vue) -- si se deshace el
        # relleno, ya no debe seguir apareciendo esa capa en la gráfica.
        rep.curva_reconectador_referencia = None
    if rep.medidor_usado == "relleno_horario":
        rep.medidor_usado = "revisar"
    rep.save()
    return _construir_detalle(frontera_id, fecha)


def recuperar_medidor(frontera_id: int, fecha: date) -> dict:
    """Dispara a demanda la misma recuperación activa (interrogar el medidor
    por WebSocket, hasta 90s) que la corrida diaria dispara sola bajo
    ciertas condiciones (curvas.curvas_de_frontera, TOLERANCIA_VALOR_
    SOSPECHOSO) -- pero para AMBOS medidores y sin ese filtro: acá es una
    decisión explícita de la persona, no necesita el gate de "incompleto o
    sospechoso".

    Solo pide la recuperación y registra el resultado en
    recuperacion_datos -- no relee la curva a mano: _construir_detalle()
    ya hace su propia lectura en vivo cada vez que se llama, así que el
    dato recuperado se refleja solo al volver a leer el detalle (este
    mismo return).

    No toca curva_final/medidor_usado/caso/editado_manualmente -- por eso
    no necesita ningún guard de "no pisar lo ya editado/validado": solo
    refresca datos de referencia (alternativa más chica que reclasificar
    la frontera completa, decidido 2026-08-20).

    Si curva_final ya viene del medidor PRINCIPAL o de CGM (Generación y
    Consumo), además revisa el respaldo recién recuperado contra la
    tolerancia de coherencia y adopta su snapshot si pasa (ver
    _revisar_respaldo_en_vivo -- MGS Agustín 1 2026-08-26: Principal ya
    correcto y automático, nunca pasa por editar_curva(), pero el
    respaldo cambió en Quoia y no había ninguna acción que lo
    reevaluara).
    """
    front, rep, Modelo = _fila_por_id(frontera_id, fecha)
    fecha_str = str(fecha)

    gaia = GaiaClient()
    try:
        borders = curvas.construir_mapa_borders(gaia)
    except Exception as e:
        raise ServicioNoDisponible(f"No se pudo consultar Quoia: {e}")

    meta = borders.get((front.codigo_frontera or "").strip().lower())
    main_meter_id = meta.get("main_meter") if meta else None
    backup_meter_id = meta.get("backup_meter") if meta else None
    if not main_meter_id and not backup_meter_id:
        raise ValidationError("Esta frontera no tiene medidor principal ni respaldo configurado en Quoia")

    def _recuperar(meter_id: int, etiqueta: str) -> str:
        resultado = recuperacion.recuperar_datos_medidor(int(meter_id), fecha_str, fecha_str)
        return f"{etiqueta}: {'éxito' if recuperacion.fue_exitosa(resultado) else 'falló'}"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futuros = []
        if main_meter_id:
            futuros.append(executor.submit(_recuperar, main_meter_id, "principal"))
        if backup_meter_id:
            futuros.append(executor.submit(_recuperar, backup_meter_id, "respaldo"))
        intentos = [f.result() for f in futuros]

    rep.recuperacion_datos = ", ".join(intentos) or None
    _revisar_respaldo_en_vivo(front, rep, fecha, Modelo is ReporteEnergiaGeneracion)
    rep.save()
    return _construir_detalle(frontera_id, fecha)


def revisar_respaldo(frontera_id: int, fecha: date) -> dict:
    """Revisa el valor EN VIVO del medidor de respaldo -- el mismo que ya
    muestra el banner "el medidor ya muestra un valor distinto en Quoia"
    en el detalle -- contra la tolerancia de coherencia, y adopta su
    snapshot si pasa (ver _revisar_respaldo_en_vivo).

    Acción explícita y liviana: NO pasa por "Recuperar medidor"
    (interrogación activa de hasta 90s al dispositivo) ni por editar
    Principal -- pensada para el botón "Usar" del banner, cuando el valor
    en vivo ya está disponible pasivamente sin necesidad de recuperación
    activa (ver MGS Agustín 1 2026-08-26: Principal ya correcto y
    automático, el respaldo cambió en Quoia y el banner ya mostraba el
    valor nuevo, pero no había ninguna acción liviana para adoptarlo).
    Generación y Consumo (extendido 2026-08-26)."""
    front, rep, Modelo = _fila_por_id(frontera_id, fecha)
    _revisar_respaldo_en_vivo(front, rep, fecha, Modelo is ReporteEnergiaGeneracion)
    rep.save()
    return _construir_detalle(frontera_id, fecha)


def eliminar_excel_terceros(frontera_id: int, fecha: date) -> dict:
    """Quita la carga de Excel de terceros de un día puntual y vuelve a
    dejar la frontera en 'Esperando Excel de terceros' -- para cuando se
    subió el archivo equivocado y no basta con re-cargar el correcto (ej. la
    fecha no debía tener ningún dato). Mismos valores que pone el
    clasificador cuando nunca se ha subido nada para ese día (ver
    FRONTERAS_TERCEROS en clasificador.py)."""
    if frontera_id not in FRONTERAS_TERCEROS:
        raise ValidationError("Esta frontera no está configurada como frontera de terceros")
    rep = ReporteEnergiaGeneracion.objects.filter(
        frontera_id=frontera_id, fecha=fecha,
    ).first()
    if rep is None:
        raise NotFound("No hay carga para eliminar en esa fecha")

    rep.caso = 0
    rep.medidor_usado = "externo"
    rep.curva_final = None
    rep.energia_final_kwh = None
    rep.curva_respaldo_terceros = None
    rep.revisar_manualmente = True
    rep.editado_manualmente = False
    rep.save()
    return _construir_detalle(frontera_id, fecha)


def curva_tipica(frontera_id: int, fecha: date) -> dict:
    """Mediana x forma horaria de los últimos días confiables -- mismo
    mecanismo que ya alimenta el relleno histórico automático (ver
    historial.py), expuesto para el botón "Curva Típica" en Corrección
    manual. No guarda nada -- solo devuelve la curva para que el usuario
    la revise/ajuste antes de "Guardar corrección"."""
    front = Frontera.objects.filter(pk=frontera_id).first()
    if front is None:
        raise NotFound("Frontera no encontrada")

    es_generacion = front.tipo_frontera == "generacion"
    if es_generacion:
        mediana, dias_usados = historial.get_mediana_generacion(frontera_id, fecha)
        forma, _ = historial.get_forma_generacion(frontera_id, fecha)
    else:
        mediana, dias_usados = historial.get_mediana_consumo(frontera_id, fecha)
        forma, _ = historial.get_forma_consumo(frontera_id, fecha)

    if mediana is None or forma is None:
        raise NotFound("No hay suficiente histórico confiable todavía para esta frontera")

    curva = escalar_curva(forma, mediana)
    return {
        "curva": curva_a_lista(curva),
        "energia_total_kwh": float(mediana),
        "dias_usados": dias_usados,
    }


def cargar_excel_terceros(frontera_id: int, contenido: bytes) -> dict:
    """Sube el Excel que envía la empresa tercera que hace el CGM de esta
    frontera (FRONTERAS_TERCEROS, ej. Cedillanos) -- reemplaza la
    transcripción manual que hoy se hace directamente en Quoia. Reporta
    'Primary' como curva_final y 'Backup' (si viene) como
    curva_respaldo_terceros, para que /enviar use ese respaldo real en vez
    de la fórmula ±1%."""
    if frontera_id not in FRONTERAS_TERCEROS:
        raise ValidationError("Esta frontera no está configurada como frontera de terceros")
    if not Frontera.objects.filter(pk=frontera_id).exists():
        raise NotFound("Frontera no encontrada")

    try:
        with transaction.atomic():
            fechas_cargadas = excel_terceros.aplicar_excel_terceros(frontera_id, contenido)
            if not fechas_cargadas:
                raise _SinFilas
    except ValueError as e:
        raise ValidationError(str(e))
    except _SinFilas:
        raise ValidationError(
            "No encontré ninguna fila 'Primary' con ENERGY TYPE = ENERGIA EXPORTADA ACTIVA"
        )

    return {"frontera_id": frontera_id, "fechas_cargadas": sorted(fechas_cargadas)}


def validar(frontera_id: int, fecha: date, usuario) -> dict:
    """Marca la frontera como revisada: `revisar_manualmente=False`.

    Es lo único que abre la puerta a `/enviar`, que exige CERO filas marcadas.
    """
    front, rep, _Modelo = _fila_por_id(frontera_id, fecha)
    rep.revisar_manualmente = False
    rep.save(update_fields=["revisar_manualmente"])
    # dict plano y no ValidarResponse: DRF recorre un modelo Pydantic como
    # pares (clave, valor) y el cuerpo sale como lista de listas. Mismo contrato
    # que app/schemas/reporte_energia.py:ValidarResponse.
    return {
        "frontera_id": frontera_id, "fecha": fecha, "revisar_manualmente": False,
    }


# ── Exclusiones temporales ───────────────────────────────────────────────────

def _exclusion_out(excl) -> dict:
    front = Frontera.objects.filter(pk=excl.frontera_id).first()
    return {
        "id": excl.id, "frontera_id": excl.frontera_id,
        "nombre_frontera": _nombre_frontera(front) if front else None,
        "motivo": excl.motivo, "fecha_inicio": excl.fecha_inicio,
        "fecha_fin_estimada": excl.fecha_fin_estimada,
        "creado_por": excl.creado_por.nombre if excl.creado_por_id else None,
        "resuelta_en": excl.resuelta_en, "created_at": excl.created_at,
    }


def listar_exclusiones(frontera_id: int) -> list[dict]:
    """Historial de exclusiones de esta frontera — activas y resueltas, más
    recientes primero."""
    return [
        _exclusion_out(f)
        for f in ReporteEnergiaExclusion.objects
        .filter(frontera_id=frontera_id)
        .select_related("creado_por")
        .order_by("-created_at")
    ]


def crear_exclusion(frontera_id: int, datos: dict, usuario) -> dict:
    """Marca una frontera para NO clasificarse en cierto rango de fechas.

    No depende de Fallas: eso exige monitoreo o representación, que no todas
    las fronteras tienen.
    """
    if datos.get("frontera_id") != frontera_id:
        raise NoProcesable("frontera_id del body no coincide con la URL")
    excl = ReporteEnergiaExclusion.objects.create(
        frontera_id=frontera_id,
        motivo=datos["motivo"],
        fecha_inicio=datos["fecha_inicio"],
        fecha_fin_estimada=datos.get("fecha_fin_estimada"),
        creado_por_id=getattr(usuario, "id", None),
    )
    return _exclusion_out(excl)


def editar_exclusion(exclusion_id: int, datos: dict) -> dict:
    excl = ReporteEnergiaExclusion.objects.filter(pk=exclusion_id).first()
    if excl is None:
        raise NotFound("Exclusión no encontrada")
    excl.motivo = datos["motivo"]
    excl.fecha_fin_estimada = datos.get("fecha_fin_estimada")
    excl.save(update_fields=["motivo", "fecha_fin_estimada"])
    return _exclusion_out(excl)


def resolver_exclusion(exclusion_id: int) -> dict:
    excl = ReporteEnergiaExclusion.objects.filter(pk=exclusion_id).first()
    if excl is None:
        raise NotFound("Exclusión no encontrada")
    excl.resuelta_en = datetime.now(timezone.utc)
    excl.save(update_fields=["resuelta_en"])
    return _exclusion_out(excl)
