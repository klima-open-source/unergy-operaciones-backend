"""El envío del reporte a Quoia y el estado de aprobación de XM.

Puerto de `/enviar` y `/estado-quoia` de `app/api/v1/reporte_energia.py`.

**Enviar está bloqueado si queda UNA sola frontera marcada para revisar.** El
reporte es del día completo: mandar la mitad deja a XM con un día incoherente.

**Solo se envían las fronteras cuyo dato tuvimos que sustituir.** Si el CGM de
Quoia ya reportó válido por su cuenta (`medidor_usado == 'cgm'`, o `caso ==
'CGM'` en consumo), no se toca: enviar de más sobreescribiría un reporte oficial
que ya estaba bien. `'excluida'` también se salta — su `curva_final` es None
mientras dure la exclusión, y sin ese chequeo se mandaría una curva de 0 kWh
FABRICADA para una frontera que justamente no debe reportar nada.

`estado_reporte` y el estado de XM son cosas distintas: el primero se llena UNA
vez al clasificar, ANTES de enviar, y sirve para decidir si el CGM automático es
válido como fuente.

**El envío corre en un hilo aparte** (`enviar_background`), igual que la
clasificación. Con ~100 fronteras llamando a Quoia una por una pasa de 2 min, y
el `--timeout 120` de gunicorn mataba el proceso a media lista: Generación (que
va primero) llegaba a Quoia, Consumo no, y como el resultado se guardaba todo
AL FINAL, no quedaba registro de nada (2026-09-29: `estado-quoia` en 0 con las
de Generación ya enviadas). Con uvicorn, antes del 2026-09-04, no había límite
y el mismo código terminaba. Ahora además cada fila se guarda apenas se envía:
si algo corta la corrida, queda escrito lo que sí salió.
"""

from __future__ import annotations

import time
import traceback
from datetime import date, datetime, timezone

from django.core.cache import cache
from django.db import close_old_connections

from apps.energia.models import ReporteEnergiaConsumo, ReporteEnergiaGeneracion
from apps.energia.services.reporte.borders import resolver_borders
# Las mismas claves y lecturas "que nunca lanzan" que usa la clasificación
# para su `en_curso`/`ultima_corrida` -- ver el bloque de caché del orquestador.
from apps.energia.services.reporte.orquestador import _cache_borrar, _cache_leer, _cache_escribir, _clave
from apps.energia.services.reporte.utils import curva_respaldo_a_reportar, reporte_ya_valido
from apps.energia.services.reporte.vistas import _nombre_frontera

# `ponytail: el cliente de Quoia sigue en app/services/mgs/`.
from app.services.mgs.gaia_client import GaiaClient


def _enviar_a_quoia(rep, front, es_generacion: bool, gaia: GaiaClient, borders: dict) -> tuple[bool | None, str | None]:
    """Envía UNA fila a Quoia (gaia.post_report) -- usado por /enviar (todas
    las fronteras del día). Antes también se reusaba desde /reportar-manual
    (una lista explícita de fronteras fuera del clasificador), endpoint
    eliminado el 2026-08-21 (commit 250558f); post_report() ya está
    verificado en producción (91/91 envíos exitosos desde entonces), no
    "sin probar en vivo" como decía la ADVERTENCIA original.

    El "Backup" que se envía sale de curva_respaldo_a_reportar() (utils.py)
    -- dato real cuando existe y es confiable (terceros, o el medidor de
    respaldo si coincide con el principal dentro de tolerancia), si no la
    estimación ±1% de siempre.

    Retorna (resultado, motivo):
    - (None, None): no hacía falta enviar, Quoia ya tenía el dato correcto
      (reporte_ya_valido) -- no se llama a Quoia para nada.
    - (True, None): envío intentado y exitoso.
    - (False, motivo): envío intentado y falló (sin border_id, Quoia
      rechazó, o excepción de red)."""
    if reporte_ya_valido(rep, es_generacion):
        return None, None

    frt_code = (front.codigo_frontera or "").strip().lower()
    meta = borders.get(frt_code)
    border_id = meta.get("id") if meta else None
    rep.enviado_quoia_en = datetime.now(timezone.utc)

    if not border_id:
        rep.enviado_quoia_ok = False
        rep.enviado_quoia_error = "Sin border_id en Quoia"
        return False, "sin border_id en Quoia"

    curva = rep.curva_final or [0.0] * 24
    main_readings = [float(v) if v is not None else 0.0 for v in curva]
    # curva_respaldo_final queda congelado desde que se fijó curva_final
    # (clasificar/editar/Excel de terceros, ver actualizar_respaldo_final())
    # -- Generación y Consumo tienen esta columna; getattr solo por si es
    # una fila de antes de que existiera, se calcula al vuelo como antes.
    backup_readings = getattr(rep, "curva_respaldo_final", None)
    if backup_readings is None:
        backup_readings, _ = curva_respaldo_a_reportar(rep)

    try:
        ok = gaia.post_report(border_id, main_readings, backup_readings)
        motivo = None if ok else "Quoia rechazó el envío"
    except Exception as exc:
        ok = False
        motivo = str(exc)

    rep.enviado_quoia_ok = ok
    rep.enviado_quoia_error = motivo
    return ok, motivo


def _etiqueta_xm(rep) -> str:
    """Traduce xm_exitoso/xm_estado (o su ausencia) a la misma etiqueta que
    muestra el dashboard de Quoia. xm_exitoso=None (sin respuesta todavía
    de get_border_report_status) es 'en_espera' -- así se ve en Quoia antes
    de que XM lo resuelva. Mapeo de 'exitoso_con_alerta' inferido (no
    confirmado con un caso real 2026-08-21): xm_exitoso=True pero
    xm_estado distinto de 'OK' (ej. 'WARNING')."""
    if rep.xm_exitoso is None:
        return "en_espera"
    if rep.xm_exitoso is False:
        return "error"
    return "exitoso" if (rep.xm_estado or "").upper() == "OK" else "exitoso_con_alerta"


MOTIVO_BLOQUEO = "Quedan fronteras con horas sin fuente (Revisar Manualmente) sin validar."

CAMPOS_ENVIO = ["enviado_quoia_en", "enviado_quoia_ok", "enviado_quoia_error"]

_TTL_ENVIO_EN_CURSO = 60 * 60       # 1h: un envío tarda minutos; es la red si el proceso muere
_TTL_ULTIMO_ENVIO = 60 * 60 * 48    # 48h, igual que ultima_corrida de la clasificación


def hay_pendientes(fecha: date) -> bool:
    return (
        ReporteEnergiaGeneracion.objects.filter(
            fecha=fecha, revisar_manualmente=True).exists()
        or ReporteEnergiaConsumo.objects.filter(
            fecha=fecha, revisar_manualmente=True).exists()
    )


def enviar(fecha: date) -> dict:
    """Envía el reporte del día a Quoia -- bloqueado si queda alguna
    frontera con 'Revisar Manualmente' pendiente (huecos sin fuente).

    Solo se envían las fronteras donde tuvimos que sustituir el dato de
    Quoia (utils.reporte_ya_valido) -- si el CGM de Quoia ya reportó válido
    por su cuenta, no se toca.

    Síncrona: la llama `enviar_background`, no el endpoint.
    """
    if hay_pendientes(fecha):
        return {
            "fecha": fecha, "enviados": 0, "fallidos": [], "bloqueado": True,
            "motivo_bloqueo": MOTIVO_BLOQUEO,
        }

    gen_filas, con_filas = _filas_del_dia(fecha)
    gaia = GaiaClient()
    borders = _borders(gaia, gen_filas + con_filas)

    enviados = 0
    fallidos: list[str] = []

    def _procesar(rep, front, es_generacion: bool) -> None:
        nonlocal enviados
        resultado, motivo = _enviar_a_quoia(rep, front, es_generacion, gaia, borders)
        if resultado is None:
            return  # ya era válido en Quoia, no hacía falta nada
        # Se guarda YA, fila por fila, incluidas las que fallaron
        # (`enviado_quoia_error` es lo que después explica el fallo). Antes era
        # un bulk_update al final, y una corrida cortada a la mitad no dejaba
        # rastro de las que sí habían llegado a Quoia.
        rep.save(update_fields=CAMPOS_ENVIO)
        if resultado:
            enviados += 1
        else:
            fallidos.append(f"{_nombre_frontera(front)} — {motivo}")

    for rep, front in gen_filas:
        _procesar(rep, front, es_generacion=True)
    for rep, front in con_filas:
        _procesar(rep, front, es_generacion=False)

    return {
        "fecha": fecha, "enviados": enviados, "fallidos": fallidos, "bloqueado": False,
    }


def _filas_del_dia(fecha: date) -> tuple[list[tuple], list[tuple]]:
    """(rep, frontera) de Generación y de Consumo, en el orden en que se envían."""
    gen_filas = [
        (rep, rep.frontera)
        for rep in ReporteEnergiaGeneracion.objects
        .filter(fecha=fecha).select_related("frontera")
    ]
    con_filas = [
        (rep, rep.frontera)
        for rep in ReporteEnergiaConsumo.objects
        .filter(fecha=fecha).select_related("frontera")
    ]
    return gen_filas, con_filas


def _borders(gaia: GaiaClient, filas: list[tuple]) -> dict:
    frt_codes = {f.codigo_frontera for _, f in filas if f.codigo_frontera}
    return resolver_borders(gaia, frt_codes) if frt_codes else {}


def simular(fecha: date) -> dict:
    """El recorrido de `enviar()` con las filas reales, SIN mandar nada.

    **No escribe en Quoia ni en la base.** Es una función aparte, y no un
    `if simulacro` dentro de enviar(), para que eso se vea leyéndola: nunca
    llama a `_enviar_a_quoia()` -- el único camino a `gaia.post_report()` --
    ni a `.save()`. Lo vigila tests/test_reporte_energia_envio_background.py.

    Lo que SÍ hace contra Quoia es leer: el login del GaiaClient y la lista de
    fronteras de `resolver_borders()`, para saber cuáles fallarían por no
    tener `border_id`.

    A diferencia de enviar(), no se detiene si hay fronteras sin validar: lo
    informa en `bloqueado` y sigue, porque ver qué saldría es justo lo útil
    antes de validarlas.
    """
    gen_filas, con_filas = _filas_del_dia(fecha)
    borders = _borders(GaiaClient(), gen_filas + con_filas)

    se_enviarian: list[dict] = []
    se_saltarian: list[dict] = []
    fallarian: list[dict] = []
    for filas, es_generacion in ((gen_filas, True), (con_filas, False)):
        for rep, front in filas:
            fila = {
                "frontera_id": front.id, "nombre": _nombre_frontera(front),
                "tipo": "generacion" if es_generacion else "consumo",
            }
            if reporte_ya_valido(rep, es_generacion):
                motivo = ("excluida" if rep.medidor_usado == "excluida"
                          else "Quoia ya tiene el CGM válido")
                se_saltarian.append({**fila, "motivo": motivo})
                continue
            meta = borders.get((front.codigo_frontera or "").strip().lower())
            if not (meta and meta.get("id")):
                fallarian.append({**fila, "motivo": "sin border_id en Quoia"})
                continue
            energia = rep.energia_final_kwh
            se_enviarian.append({
                **fila, "energia_kwh": float(energia) if energia is not None else None,
            })

    bloqueado = hay_pendientes(fecha)
    return {
        "fecha": fecha, "simulacro": True, "bloqueado": bloqueado,
        **({"motivo_bloqueo": MOTIVO_BLOQUEO} if bloqueado else {}),
        "se_enviarian": se_enviarian, "se_saltarian": se_saltarian, "fallarian": fallarian,
        # Mismas claves que un envío real, para que el front las lea igual.
        "enviados": 0, "fallidos": [f"{f['nombre']} — {f['motivo']}" for f in fallarian],
    }


def tomar_envio(fecha: date) -> bool:
    """Marca "hay un envío andando" para la fecha; False si ya había otro.

    La marca se toma en el ENDPOINT, antes de lanzar el hilo, y la libera el
    hilo al terminar: así, en cuanto `envio_en_curso()` vuelve a None, el
    resultado nuevo ya está escrito, y el front no puede leer el del envío
    anterior por llegar antes que el hilo. `cache.add` es SETNX: dos clics
    simultáneos no pasan los dos. Falla hacia "seguir" si Redis no responde,
    mismo criterio que orquestador._tomar_en_curso.
    """
    valor = {"desde": datetime.now(timezone.utc).isoformat()}
    try:
        return bool(cache.add(_clave("envio_en_curso", fecha), valor, _TTL_ENVIO_EN_CURSO))
    except Exception as exc:
        print(f"[reporte_energia] cache no disponible al marcar envio_en_curso fecha={fecha}: {exc}")
        return True


def envio_en_curso(fecha: date) -> dict | None:
    return _cache_leer("envio_en_curso", fecha)


def ultimo_envio(fecha: date) -> dict | None:
    return _cache_leer("ultimo_envio", fecha)


def enviar_background(fecha: date, simulacro: bool = False) -> None:
    """`enviar()` -- o `simular()` -- en un hilo aparte, con el resultado a la
    caché. Quien la llama ya tomó la marca con `tomar_envio()`; acá solo se
    libera. El simulacro comparte marca y resultado con el envío real: así
    no corren los dos a la vez, y el front lee los dos con la misma consulta
    (`simulacro: true` los distingue)."""
    close_old_connections()
    inicio = time.monotonic()
    try:
        resultado = simular(fecha) if simulacro else enviar(fecha)
        print(
            f"[reporte_energia] enviar_background fecha={fecha} simulacro={simulacro} "
            f"enviados={resultado['enviados']} fallidos={len(resultado['fallidos'])} "
            f"bloqueado={resultado['bloqueado']}"
        )
        _cache_escribir("ultimo_envio", fecha, {
            **resultado, "fecha": str(fecha), "simulacro": simulacro,
            "duracion_s": round(time.monotonic() - inicio, 1),
            "terminado_en": datetime.now(timezone.utc).isoformat(),
        }, _TTL_ULTIMO_ENVIO)
    except Exception:
        print(f"[reporte_energia] enviar_background fecha={fecha} simulacro={simulacro} FALLÓ:")
        print(traceback.format_exc())
        _cache_escribir("ultimo_envio", fecha, {
            "fecha": str(fecha), "simulacro": simulacro,
            "enviados": 0, "fallidos": [], "bloqueado": False,
            "duracion_s": round(time.monotonic() - inicio, 1),
            "terminado_en": datetime.now(timezone.utc).isoformat(),
            "error_general": (
                "El simulacro se interrumpió; no se mandó nada a Quoia. Ver logs."
                if simulacro else
                "El envío se interrumpió. Lo que alcanzó a salir quedó registrado "
                "en cada frontera; ver logs."
            ),
        }, _TTL_ULTIMO_ENVIO)
    finally:
        _cache_borrar("envio_en_curso", fecha)
        close_old_connections()


def _fronteras_enviadas(fecha: date) -> list[tuple]:
    """(rep, front, tipo) de toda fila con enviado_quoia_en no nulo para la
    fecha -- las que de verdad se intentaron mandar a Quoia."""
    gen_filas = list(
        ReporteEnergiaGeneracion.objects
        .filter(fecha=fecha, enviado_quoia_en__isnull=False)
        .select_related("frontera")
    )
    con_filas = list(
        ReporteEnergiaConsumo.objects
        .filter(fecha=fecha, enviado_quoia_en__isnull=False)
        .select_related("frontera")
    )
    return ([(rep, rep.frontera, "generacion") for rep in gen_filas]
            + [(rep, rep.frontera, "consumo") for rep in con_filas])


def _conteos(filas) -> tuple[dict, list[dict]]:
    conteos = {"en_espera": 0, "exitoso": 0, "exitoso_con_alerta": 0, "error": 0}
    fallidas: list[dict] = []
    for rep, front, tipo in filas:
        etiqueta = _etiqueta_xm(rep)
        conteos[etiqueta] += 1
        if etiqueta == "error":
            fallidas.append({
                "frontera_id": front.id,
                "nombre_proyecto": _nombre_frontera(front),
                "tipo": tipo,
            })
    return conteos, fallidas


def estado_quoia_actual(fecha: date) -> dict:
    """El estado de XM YA GUARDADO, sin volver a consultar Quoia.

    Rápido y seguro de llamar al abrir la vista; para forzar una revisión en
    vivo está `estado_quoia_revisar`.
    """
    filas = _fronteras_enviadas(fecha)
    conteos, fallidas = _conteos(filas)
    return {"fecha": fecha, "total": len(filas), "fallidas": fallidas, **conteos}


CAMPOS_XM = ["xm_verificado_en", "xm_process_id", "xm_estado", "xm_exitoso"]

# Lo que una revisión puede gastar consultando Quoia antes de cortar y
# responder. Sale del `--timeout 120` de gunicorn menos el TIMEOUT de 30 s de
# una sola llamada del GaiaClient, con margen: si la última consulta arranca
# justo antes del corte y agota su timeout, la petición igual termina a tiempo.
_PRESUPUESTO_REVISION_S = 75


def estado_quoia_revisar(fecha: date, presupuesto_s: float = _PRESUPUESTO_REVISION_S) -> dict:
    """Consulta Quoia para las filas enviadas que aún no tienen respuesta.

    Solo vuelve a golpear Quoia para las que están en espera (`xm_exitoso is
    None`): está pensado para dispararse justo después de `/enviar` y llamarse
    cada tanto hasta que nadie quede en espera.

    **Revisa por tandas.** Es una llamada a Quoia por frontera, y con ~100
    pasaba del `--timeout 120` de gunicorn: el proceso moría, el bulk_update
    del final no corría y el front -- que ignora ese error en silencio -- nunca
    mostraba el panel (2026-09-30, el mismo modo de fallo que el envío del
    29-sep). Ahora cada fila se guarda apenas Quoia responde, y al pasar
    `presupuesto_s` se corta y se responde con lo que hay; `sin_revisar` dice
    cuántas quedaron para la próxima llamada del polling. Van primero las que
    nunca se miraron y después las que hace más tiempo no se miran, para que
    una tanda no repita siempre las mismas.
    """
    filas = _fronteras_enviadas(fecha)
    pendientes = [f for f in filas if f[0].xm_exitoso is None]
    pendientes.sort(key=lambda f: (
        f[0].xm_verificado_en is not None,
        f[0].xm_verificado_en or datetime.min.replace(tzinfo=timezone.utc),
    ))

    sin_revisar = 0
    if pendientes:
        inicio = time.monotonic()
        gaia = GaiaClient()
        borders = _borders(gaia, [(rep, front) for rep, front, _ in pendientes])
        for i, (rep, front, _tipo) in enumerate(pendientes):
            if time.monotonic() - inicio > presupuesto_s:
                sin_revisar = len(pendientes) - i
                break
            meta = borders.get((front.codigo_frontera or "").strip().lower())
            border_id = meta.get("id") if meta else None
            estado = gaia.get_border_report_status(border_id, str(fecha)) if border_id else None
            rep.xm_verificado_en = datetime.now(timezone.utc)
            if estado:
                rep.xm_process_id = estado.get("xm_process_id")
                rep.xm_estado = estado.get("status")
                rep.xm_exitoso = estado.get("success")
            rep.save(update_fields=CAMPOS_XM)

    conteos, fallidas = _conteos(filas)
    return {
        "fecha": fecha, "total": len(filas), "fallidas": fallidas,
        "sin_revisar": sin_revisar, **conteos,
    }
