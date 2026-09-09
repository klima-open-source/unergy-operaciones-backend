"""Orquestador del reporte de energía: corre un día completo, frontera por frontera.

Puerto de `app/services/reporte_energia/orquestador.py`.

**Quoia es la fuente de verdad de QUÉ frontera reporta al ASIC**, no una regla
propia. `codigos_quoia` es el conjunto de `frt_code` que trae
`gaia.get_all_borders()`, lo mismo que alimenta la vista "Reportes" de Quoia
Manager. La regla anterior (`estado == en_operacion AND srv_cgm`, con una
excepción manual) se descartó el 2026-08-21: comparadas contra datos reales, 0
fronteras se habrían perdido y esa regla SÍ tenía huecos (GD Piojó, GD La
Hormiguita ya registradas en Quoia pero nunca marcadas a mano).

**Una excepción en UNA frontera no tumba la corrida.** El 2026-08-02 una sola
frontera de consumo con `energia_final_kwh` NULL en su histórico mató la corrida
entera después de 8 de 103. Ahora se registra, se marca la fila para revisar y se
sigue con la siguiente.

**Un registro `editado_manualmente` no se pisa** con el resultado automático de
una re-ejecución: el reporte semiautomático depende de eso.

**El estado de la corrida es compartido, la cancelación no.** El resultado de
la última corrida y la bandera de "Detener" viven en la caché (el Redis de
Celery), no en la memoria del proceso: con `--workers 3` el clic de "Detener"
caía en cualquiera de los tres y respondía OK sin detener nada. Pero **solo las
corridas manuales consultan la bandera**: la de Celery corre con
`cancelable=False` y no la lee ni una vez, así que sigue siendo tan intocable
como cuando el estado era un diccionario en su propia memoria.

Cada fila se guarda en cuanto se calcula (`fila.save()`), así que el avance ya es
visible en /fronteras mientras el resto sigue corriendo — el original hacía
`commit()` cada 5 fronteras para lo mismo.
"""

from __future__ import annotations

import traceback
from datetime import date, datetime, timezone

from django.core.cache import cache
from django.db.models import Q

from apps.energia.models import (
    ReporteEnergiaConsumo, ReporteEnergiaExclusion, ReporteEnergiaGeneracion,
)
from apps.energia.services.reporte import (
    clasificador, clasificador_consumo, curvas, historial,
)
from apps.energia.services.reporte.utils import actualizar_respaldo_final, curva_a_lista
from apps.fronteras.models import Frontera

# `ponytail: los clientes de Quoia y SolarView siguen en app/services/mgs/`.
from app.services.mgs.gaia_client import GaiaClient
from app.services.mgs.solarview_client import SolarViewClient

TIPOS_GENERACION = {"generacion"}
TIPOS_CONSUMO = {"consumo", "consumo_auxiliar", "consumo_propio"}

# Resultado de la última corrida por fecha (en memoria -- se pierde si el
# proceso reinicia, o no se ve entre workers si hubiera más de uno) y por eso
# ahora vive en la caché compartida (el Redis de Celery, ver CACHES en
# settings.py). Tres claves, todas por fecha:
#
#   · ultima_corrida -- cómo terminó. La ESCRIBEN las dos corridas (la manual
#     y la de Celery de las 3:30) y la lee GET /reporte-energia/ejecutar/estado.
#     Antes, la de Celery guardaba su resultado en la memoria de OTRO
#     contenedor, así que ese endpoint no podía saber nada de ella ni por
#     casualidad: si la corrida automática terminaba con fronteras fallidas,
#     eso solo existía en los logs.
#   · cancelar -- la bandera cooperativa de "Detener": el loop la revisa entre
#     frontera y frontera (no corta a medio proceso de una, solo entre una y
#     la siguiente).
#   · en_curso -- para que dos corridas de la misma fecha no se pisen.
#
# **Solo las corridas CANCELABLES leen la bandera** (ver ejecutar_dia). La de
# Celery corre con cancelable=False y no la mira ni una vez: conserva la
# propiedad que tenía cuando el estado era un dict en su propia memoria --
# nada externo la puede detener. Mover esa lectura al bucle automático habría
# sido cambiar un `dict.get()` infalible por ~100 consultas a Redis por
# corrida, y le habría dado a un clic el poder de matar la corrida de la
# madrugada. El "Detener" que sí hacía falta arreglar es el de las manuales.
#
# La caché NO es durable (Redis del compose sin persistencia): un reinicio
# borra las tres. Es el mismo alcance efímero que tenían como diccionarios,
# solo que ahora compartido. Nada de esto es dato de negocio.
_TTL_ULTIMA_CORRIDA = 60 * 60 * 48   # 48h: el reporte del día se revisa a la mañana siguiente
_TTL_CANCELAR = 60 * 60 * 2          # 2h: una corrida tarda 23-50 min; más que eso es basura vieja
_TTL_EN_CURSO = 60 * 60 * 2          # igual, y se libera en el finally


def _clave(nombre: str, fecha: date) -> str:
    return f"reporte_energia:{nombre}:{fecha}"


def _cache_leer(nombre: str, fecha: date):
    """Lectura que NUNCA lanza: si Redis no responde, es como si no hubiera
    nada guardado. Es lo que mantiene la corrida a salvo -- una lectura que
    propague la excepción mataría el bucle a media lista de fronteras, y la
    caída de Redis no tiene nada que ver con la clasificación."""
    try:
        return cache.get(_clave(nombre, fecha))
    except Exception as exc:
        print(f"[reporte_energia] cache no disponible al leer {nombre} fecha={fecha}: {exc}")
        return None


def _cache_escribir(nombre: str, fecha: date, valor, ttl: int) -> None:
    """Escritura que NUNCA lanza -- mismo criterio que _cache_leer()."""
    try:
        cache.set(_clave(nombre, fecha), valor, ttl)
    except Exception as exc:
        print(f"[reporte_energia] cache no disponible al escribir {nombre} fecha={fecha}: {exc}")


def _cache_borrar(nombre: str, fecha: date) -> None:
    try:
        cache.delete(_clave(nombre, fecha))
    except Exception as exc:
        print(f"[reporte_energia] cache no disponible al borrar {nombre} fecha={fecha}: {exc}")


def ultima_corrida(fecha: date) -> dict | None:
    return _cache_leer("ultima_corrida", fecha)


def cancelar_corrida(fecha: date) -> None:
    _cache_escribir("cancelar", fecha, True, _TTL_CANCELAR)


def corrida_en_curso(fecha: date) -> dict | None:
    """Quién está corriendo esa fecha ahora mismo, si alguien -- {'origen',
    'desde'}. Lo consulta el endpoint /ejecutar ANTES de lanzar el hilo, para
    poder responder con un mensaje en vez de arrancar una segunda corrida que
    escribiría las mismas filas en paralelo."""
    return _cache_leer("en_curso", fecha)


def _fronteras_con_reporte(codigos_quoia: set[str]) -> list[tuple[Frontera, str | None, float | None]]:
    """(Frontera, project_id_solarview) de las fronteras que de verdad
    reportan al ASIC -- 'activa'/'deleted_at' son nuestro propio control de
    desactivación (una frontera que marcamos inactiva no reporta aunque
    Quoia la siga teniendo registrada).

    Para decidir SI reporta, la fuente de verdad es Quoia mismo:
    codigos_quoia es el conjunto de frt_code (lowercase) que trae
    gaia.get_all_borders() -- lo mismo que alimenta la vista "Reportes" de
    Quoia Manager. Antes se usaba una regla propia (Proyecto.estado ==
    en_operacion AND srv_cgm, con una excepción manual
    reportar_asic_forzado) -- se descartó 2026-08-21 tras comparar ambos
    conjuntos contra datos reales: 0 fronteras se habrían perdido, y esa
    regla propia SÍ tenía huecos reales (GD Piojó, GD La Hormiguita: ya
    registradas en Quoia pero nunca marcadas a mano)."""
    filas = (
        Frontera.objects
        .filter(estado="activa", codigo_frontera__isnull=False, deleted_at__isnull=True)
        .select_related("proyecto")
    )
    return [
        (
            f,
            f.proyecto.project_id_solarview if f.proyecto_id else None,
            f.proyecto.potencia_instalada_kwp if f.proyecto_id else None,
        )
        for f in filas
        if f.codigo_frontera.strip().lower() in codigos_quoia
    ]


def _exclusion_activa(frontera_id: int, fecha: date) -> ReporteEnergiaExclusion | None:
    """Exclusión temporal vigente para esta frontera+fecha (ej. CT en falla
    ya reportado a XM) -- ver ReporteEnergiaExclusion. Si existe, el
    clasificador ni siquiera se llama para esta frontera ese día."""
    return (
        ReporteEnergiaExclusion.objects
        .filter(
            frontera_id=frontera_id,
            resuelta_en__isnull=True,
            fecha_inicio__lte=fecha,
        )
        .filter(Q(fecha_fin_estimada__isnull=True) | Q(fecha_fin_estimada__gte=fecha))
        .first()
    )


def _upsert_generacion(frontera_id: int, fecha: date, resultado: dict) -> None:
    existente = ReporteEnergiaGeneracion.objects.filter(
        frontera_id=frontera_id, fecha=fecha,
    ).first()

    if existente is not None and existente.editado_manualmente:
        # No se pisa una corrección manual con el resultado automático de
        # una re-ejecución -- el reporte semiautomático depende de esto.
        return

    fila = existente or ReporteEnergiaGeneracion(frontera_id=frontera_id, fecha=fecha)
    fila.caso = resultado["caso"]
    fila.medidor_usado = resultado.get("medidor_usado")
    fila.energia_final_kwh = resultado.get("energia_final_kwh")
    fila.curva_final = curva_a_lista(resultado.get("curva_final"))
    # Se calcula y persiste SIEMPRE (no solo cuando el Caso ganador lo usó
    # para 'energia_final_kwh') -- para que 'Reportar con otra fuente' en el
    # front pueda ofrecer "Inversores × FP" sin importar el Caso del día,
    # mientras Solenium tenga dato completo (pedido 2026-09-02). Que 'fp'
    # tenga valor NO implica que influyó en el número de hoy -- el front
    # decide si mostrarlo como "el FP de hoy" mirando medidor_usado/
    # horas_rellenadas_solenium (ver ReporteEnergiaDetalleTab.vue). No
    # aplica a Caso 0 (tercero) -- no hay nada que "perder por FP" ahí.
    fp, fp_calc = resultado.get("fp"), resultado.get("fp_calculada")
    if fp is None and resultado.get("caso") != 0:
        fp, fp_calc = historial.get_factor_perdida_detalle(frontera_id, fecha)
    fila.fp = fp
    fila.fp_calculada = fp_calc
    fila.error_final_pct = resultado.get("error_final_pct")
    fila.energia_cgm_kwh = resultado.get("energia_cgm_kwh")
    fila.estado_reporte = resultado.get("estado_reporte")
    fila.energia_solenium_kwh = resultado.get("energia_solenium_kwh")
    fila.solenium_completo = resultado.get("solenium_completo")
    fila.nota_solenium = resultado.get("nota_solenium")
    fila.horas_rellenadas_reconectador = resultado.get("horas_rellenadas_reconectador")
    fila.horas_rellenadas_solenium = resultado.get("horas_rellenadas_solenium")
    fila.horas_rellenadas_historico = resultado.get("horas_rellenadas_historico")
    fila.horas_rellenadas_medidor_cruzado = resultado.get("horas_rellenadas_medidor_cruzado")
    fila.recuperacion_datos = resultado.get("recuperacion_datos")
    fila.revisar_manualmente = bool(resultado.get("revisar_manualmente", False))
    fila.energia_medidor_principal_kwh = resultado.get("energia_medidor_principal_kwh")
    fila.energia_medidor_respaldo_kwh = resultado.get("energia_medidor_respaldo_kwh")
    fila.medidor_principal_completo = resultado.get("medidor_principal_completo")
    fila.medidor_respaldo_completo = resultado.get("medidor_respaldo_completo")
    fila.error_clasificacion = resultado.get("error_clasificacion")
    # clasificador.py SÍ calcula estas curvas de referencia (tal como
    # estaban al momento de clasificar, ver comentario ahí sobre El Paso
    # Norte 2026-08-05) pero nunca se estaban copiando a la fila -- por eso
    # 'Detalle de las fuentes' seguía cayendo a la consulta en vivo aunque
    # la fila fuera de hoy, y nunca mostraba el aviso de "Quoia ya cambió"
    # (MGS 0028 Chiriguaná Norte 1 2026-08-10: medidor principal usado en
    # 6.686,2 kWh al clasificar, pero en vivo ya mostraba 10.300,3 kWh, sin
    # ningún aviso que lo explicara).
    fila.curva_medidor_principal = resultado.get("curva_medidor_principal")
    fila.curva_medidor_respaldo = resultado.get("curva_medidor_respaldo")
    fila.curva_cgm_referencia = resultado.get("curva_cgm_referencia")
    fila.curva_solenium_referencia = resultado.get("curva_solenium_referencia")
    # Solo se pone si el reconectador SÍ se llegó a consultar ese día
    # (medidor+inversores ya dejaron huecos) -- a diferencia de medidor/
    # Solenium arriba, NO se asigna None cuando no aplica: un JSONB en
    # SQLAlchemy guarda Python None como el literal JSON 'null' (no como
    # SQL NULL), así que asignarlo siempre dejaba la columna "no nula" para
    # básicamente todas las filas -- is_not(None) en SQL no servía para
    # encontrar cuáles SÍ tenían dato real (descubierto 2026-08-21).
    if resultado.get("curva_reconectador_referencia") is not None:
        fila.curva_reconectador_referencia = resultado["curva_reconectador_referencia"]
    # Sin curva_final no hay nada que comparar/reportar (ej. 'excluida',
    # curva_final=None a propósito) -- dejar curva_respaldo_final en None
    # también, en vez de persistir un "estimado" de puros ceros.
    if fila.curva_final is not None:
        actualizar_respaldo_final(fila)
    fila.save()


def _upsert_consumo(frontera_id: int, fecha: date, resultado: dict) -> None:
    existente = ReporteEnergiaConsumo.objects.filter(
        frontera_id=frontera_id, fecha=fecha,
    ).first()

    if existente is not None and existente.editado_manualmente:
        return

    fila = existente or ReporteEnergiaConsumo(frontera_id=frontera_id, fecha=fecha)
    fila.caso = resultado["caso"]
    fila.medidor_usado = resultado.get("medidor_usado")
    fila.energia_final_kwh = resultado.get("energia_final_kwh")
    fila.curva_final = curva_a_lista(resultado.get("curva_final"))
    fila.energia_cgm_kwh = resultado.get("energia_cgm_kwh")
    fila.estado_reporte = resultado.get("estado_reporte")
    fila.horas_rellenadas_historico = resultado.get("horas_rellenadas_historico")
    fila.horas_rellenadas_medidor_cruzado = resultado.get("horas_rellenadas_medidor_cruzado")
    fila.recuperacion_datos = resultado.get("recuperacion_datos")
    fila.revisar_manualmente = bool(resultado.get("revisar_manualmente", False))
    fila.error_clasificacion = resultado.get("error_clasificacion")
    # Mismo fix que _upsert_generacion -- clasificador_consumo.py sí calcula
    # estas curvas de referencia pero nunca se estaban copiando a la fila.
    fila.curva_medidor_principal = resultado.get("curva_medidor_principal")
    fila.curva_medidor_respaldo = resultado.get("curva_medidor_respaldo")
    fila.curva_cgm_referencia = resultado.get("curva_cgm_referencia")
    # Mismo criterio que _upsert_generacion -- sin curva_final no hay nada
    # que comparar/reportar.
    if fila.curva_final is not None:
        actualizar_respaldo_final(fila)
    fila.save()


def _marcar_error_generacion(frontera_id: int, fecha: date, error_msg: str) -> None:
    """Se llama cuando clasificar_generacion() lanzó una excepción -- deja
    una fila explícita marcada para revisar en vez de dejar la frontera
    ausente del reporte del día (ver comentario en el loop de ejecutar_dia)."""
    existente = ReporteEnergiaGeneracion.objects.filter(
        frontera_id=frontera_id, fecha=fecha,
    ).first()
    if existente is not None:
        if existente.editado_manualmente:
            return
        existente.revisar_manualmente = True
        existente.error_clasificacion = error_msg[:500]
        existente.save(update_fields=["revisar_manualmente", "error_clasificacion"])
        return
    ReporteEnergiaGeneracion.objects.create(
        frontera_id=frontera_id, fecha=fecha, caso=-1,
        revisar_manualmente=True, error_clasificacion=error_msg[:500],
    )


def _marcar_error_consumo(frontera_id: int, fecha: date, error_msg: str) -> None:
    existente = ReporteEnergiaConsumo.objects.filter(
        frontera_id=frontera_id, fecha=fecha,
    ).first()
    if existente is not None:
        if existente.editado_manualmente:
            return
        existente.revisar_manualmente = True
        existente.error_clasificacion = error_msg[:500]
        existente.save(update_fields=["revisar_manualmente", "error_clasificacion"])
        return
    ReporteEnergiaConsumo.objects.create(
        frontera_id=frontera_id, fecha=fecha, caso="Error",
        revisar_manualmente=True, error_clasificacion=error_msg[:500],
    )


def ejecutar_dia(fecha: date, cancelable: bool = False) -> dict:
    """Corre la clasificación de Generación y Consumo para todas las
    fronteras activas, para una fecha dada, y guarda el resultado.

    Retorna un resumen {'generacion': {...}, 'consumo': {...}} con conteos
    por caso, para log/depuración -- el detalle real vive en la BD.

    `cancelable`: si "Detener" puede pararla. True solo para las corridas que
    lanza una persona desde POST /ejecutar. La de Celery de las 3:30 usa el
    default y **no consulta la bandera en ninguna iteración** -- ni gasta
    ~100 lecturas de Redis por corrida, ni le da a un clic la posibilidad de
    matar la clasificación de la madrugada. Para pararla sigue estando
    `docker compose restart worker`, igual que siempre.
    """
    gaia = GaiaClient()
    sv = SolarViewClient()

    bordes = curvas.construir_mapa_borders(gaia)
    mapa_medidor_nodo = curvas.construir_mapa_medidor_nodo(gaia)

    resumen_gen: dict[str, int] = {}
    resumen_con: dict[str, int] = {}
    omitidas: list[str] = []
    fallidas: list[str] = []

    fronteras = _fronteras_con_reporte(set(bordes.keys()))
    print(f"[reporte_energia] ejecutar_dia fecha={fecha}: {len(fronteras)} fronteras activas")

    # Limpia cualquier cancelación pendiente de una corrida anterior -- sin
    # esto, un "Detener" que quedara guardado mataría la siguiente corrida de
    # esa fecha después de la primera frontera. Solo aplica a las cancelables:
    # la automática no lee la bandera, así que tampoco tiene nada que limpiar.
    if cancelable:
        _cache_borrar("cancelar", fecha)
    cancelado = False

    for i, (frontera, project_id_solarview, potencia_instalada_kwp) in enumerate(fronteras, start=1):
        if cancelable and _cache_leer("cancelar", fecha):
            print(f"[reporte_energia] ejecutar_dia fecha={fecha}: detenido manualmente en {i}/{len(fronteras)}")
            cancelado = True
            break

        frt_code = frontera.codigo_frontera.strip().lower()
        border_meta = bordes.get(frt_code)
        pid_solarview = int(project_id_solarview) if project_id_solarview and project_id_solarview.isdigit() else None

        excl = _exclusion_activa(frontera.id, fecha)
        if excl is not None:
            # No se llama al clasificador para nada -- ni CGM, ni medidor,
            # ni crudos. Ver ReporteEnergiaExclusion (ej. CT en falla ya
            # reportado a XM, mientras se resuelve no se reporta ningún
            # número automático).
            nota = f"Excluida temporalmente -- {excl.motivo}"
            # revisar_manualmente=False a propósito -- el motivo ya está
            # explicado en error_clasificacion, no hace falta que alguien lo
            # revise, y dejarlo en True bloquearía /enviar para TODAS las
            # demás fronteras ese día (el gate de envío revisa cualquier fila
            # con revisar_manualmente=True, sin importar el caso).
            if frontera.tipo_frontera in TIPOS_GENERACION:
                resultado = {
                    "caso": -2, "energia_final_kwh": None, "curva_final": None,
                    "medidor_usado": "excluida", "revisar_manualmente": False,
                    "error_clasificacion": nota,
                }
                _upsert_generacion(frontera.id, fecha, resultado)
                resumen_gen["-2"] = resumen_gen.get("-2", 0) + 1
            elif frontera.tipo_frontera in TIPOS_CONSUMO:
                resultado = {
                    "caso": "Excluida", "energia_final_kwh": None, "curva_final": None,
                    "medidor_usado": "excluida", "revisar_manualmente": False,
                    "error_clasificacion": nota,
                }
                _upsert_consumo(frontera.id, fecha, resultado)
                resumen_con["Excluida"] = resumen_con.get("Excluida", 0) + 1
            continue

        # Una excepcion no manejada en UNA frontera (bug del clasificador,
        # dato historico corrupto, timeout puntual de Quoia/Solenium) no debe
        # tumbar la corrida completa de las demas -- se registra, se hace
        # rollback para dejar la sesion limpia, y se sigue con la siguiente.
        # Real: 2026-08-02 una sola frontera de Consumo con energia_final_kwh
        # NULL en su historico mato la corrida entera despues de solo 8/103.
        try:
            if frontera.tipo_frontera in TIPOS_GENERACION:
                resultado = clasificador.clasificar_generacion(
                    gaia, sv, frontera.id, frt_code, border_meta, pid_solarview, mapa_medidor_nodo, fecha,
                    capacidad_efectiva_mw=(
                        float(potencia_instalada_kwp) / 1000 if potencia_instalada_kwp is not None else None
                    ),
                )
                _upsert_generacion(frontera.id, fecha, resultado)
                clave = str(resultado["caso"])
                resumen_gen[clave] = resumen_gen.get(clave, 0) + 1

            elif frontera.tipo_frontera in TIPOS_CONSUMO:
                resultado = clasificador_consumo.clasificar_consumo(
                    gaia, frontera.id, frt_code, border_meta, mapa_medidor_nodo, fecha,
                )
                _upsert_consumo(frontera.id, fecha, resultado)
                clave = str(resultado["caso"])
                resumen_con[clave] = resumen_con.get(clave, 0) + 1

            else:
                omitidas.append(f"{frontera.nombre_frontera} ({frontera.tipo_frontera})")
                continue
        except Exception as exc:
            fallidas.append(frontera.nombre_frontera)
            print(f"[reporte_energia]   ({i}/{len(fronteras)}) {frt_code} -> FALLÓ, se sigue con las demás:")
            print(traceback.format_exc())
            try:
                if frontera.tipo_frontera in TIPOS_GENERACION:
                    _marcar_error_generacion(frontera.id, fecha, str(exc))
                elif frontera.tipo_frontera in TIPOS_CONSUMO:
                    _marcar_error_consumo(frontera.id, fecha, str(exc))
            except Exception:
                # Si ni siquiera esto se pudo guardar, no vale la pena tumbar
                # la corrida por eso -- la frontera igual queda en 'fallidas'
                # para el log/alerta, aunque no aparezca marcada en la BD.
                print(f"[reporte_energia]   {frt_code} -> tampoco se pudo registrar el error en la BD:")
                print(traceback.format_exc())
            continue

        print(f"[reporte_energia]   ({i}/{len(fronteras)}) {frt_code} -> caso {clave}")

    if cancelable:
        _cache_borrar("cancelar", fecha)
    return {
        "generacion": resumen_gen, "consumo": resumen_con,
        "omitidas": omitidas, "fallidas": fallidas, "fecha": str(fecha),
        "cancelado": cancelado,
    }


def _tomar_en_curso(fecha: date, origen: str, exclusivo: bool) -> bool:
    """Marca la fecha como "hay una corrida andando", y con `exclusivo` se
    niega si ya había otra. Retorna si se puede seguir.

    `exclusivo` es True solo para las corridas manuales: **la automática nunca
    cede el paso**, es la que tiene el horario. Si a las 3:30 había una manual
    en curso, la automática pisa la marca y arranca igual (que es lo que pasa
    hoy, sin marca de ninguna clase).

    Falla hacia "seguir": si Redis no responde no se sabe si hay otra corrida,
    y entre no clasificar el día y arriesgar dos corridas simultáneas -- lo
    que ya pasa hoy -- lo segundo es mucho menos grave.

    `cache.add` es SETNX en Redis: la comprobación y la escritura son una sola
    operación atómica, así que dos clics simultáneos no pueden pasar los dos.
    """
    valor = {"origen": origen, "desde": datetime.now(timezone.utc).isoformat()}
    try:
        if exclusivo:
            return bool(cache.add(_clave("en_curso", fecha), valor, _TTL_EN_CURSO))
        cache.set(_clave("en_curso", fecha), valor, _TTL_EN_CURSO)
        return True
    except Exception as exc:
        print(f"[reporte_energia] cache no disponible al marcar en_curso fecha={fecha}: {exc}")
        return True


def ejecutar_dia_background(fecha: date, cancelable: bool = False) -> None:
    """Igual que ejecutar_dia(), pero abre su propia sesión de BD y corre en
    un hilo aparte -- pensada para que el endpoint /ejecutar responda de
    inmediato en vez de bloquear el request.

    Con ~50 fronteras (varias llamadas a Quoia/Solenium cada una, más hasta
    90s de recuperación activa por medidor incompleto) una corrida completa
    puede tardar varios minutos -- más que el timeout fijo del proxy externo
    de Vercel (~30s), así que no puede devolverse en el mismo request.

    `cancelable`: lo pasa el endpoint (True) y no la tarea de Celery (False,
    el default) -- ver ejecutar_dia(). Decide también quién cede el paso si
    ya hay una corrida de esa fecha andando: la manual se retira, la
    automática nunca.
    """
    from django.db import close_old_connections

    origen = "manual" if cancelable else "automatica"
    if not _tomar_en_curso(fecha, origen, exclusivo=cancelable):
        otra = corrida_en_curso(fecha) or {}
        print(
            f"[reporte_energia] ejecutar_dia_background fecha={fecha} NO ARRANCA: "
            f"ya hay una corrida {otra.get('origen', 'desconocida')} desde {otra.get('desde', '?')}"
        )
        return

    print(f"[reporte_energia] ejecutar_dia_background fecha={fecha} origen={origen} ARRANCÓ")
    # Hilo propio: la conexión de este hilo puede venir muerta de una corrida
    # anterior y Django no la recicla solo fuera del ciclo de request.
    close_old_connections()
    try:
        resultado = ejecutar_dia(fecha, cancelable=cancelable)
        # print() en vez de logging -- en este contenedor los logs de nivel
        # INFO del módulo logging no se están capturando (solo llega un
        # WARNING+ vía el handler de último recurso), igual que el patrón
        # "[startup] ..." que ya usa el resto del backend con print().
        print(
            f"[reporte_energia] ejecutar_dia_background fecha={fecha} "
            f"generacion={resultado['generacion']} consumo={resultado['consumo']} "
            f"omitidas={len(resultado['omitidas'])} fallidas={resultado['fallidas']} "
            f"cancelado={resultado['cancelado']}"
        )
        _cache_escribir("ultima_corrida", fecha, {
            "terminado_en": datetime.now(timezone.utc).isoformat(),
            "origen": origen,
            "fallidas": resultado["fallidas"],
            "omitidas": resultado["omitidas"],
            "cancelado": resultado["cancelado"],
        }, _TTL_ULTIMA_CORRIDA)
    except Exception:
        print(f"[reporte_energia] ejecutar_dia_background fecha={fecha} FALLÓ:")
        print(traceback.format_exc())
        _cache_escribir("ultima_corrida", fecha, {
            "terminado_en": datetime.now(timezone.utc).isoformat(),
            "origen": origen,
            "fallidas": [], "omitidas": [],
            "error_general": "La corrida se interrumpió por completo -- ver logs.",
        }, _TTL_ULTIMA_CORRIDA)
    finally:
        # Se libera sin preguntar de quién era. Si una manual y la automática
        # se solaparon (la automática pisa la marca, ver _tomar_en_curso), la
        # primera en terminar la borra y la otra queda sin marca -- o sea, el
        # comportamiento de hoy, donde no hay marca en absoluto. El TTL de 2h
        # es la red por si el proceso muere sin pasar por acá.
        _cache_borrar("en_curso", fecha)
        close_old_connections()
