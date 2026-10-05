"""El envío del reporte a Quoia y su resumen.

Puerto de `/enviar` de `app/api/v1/reporte_energia.py`.

**Enviar está bloqueado si queda UNA sola frontera marcada para revisar.** El
reporte es del día completo: mandar la mitad deja a XM con un día incoherente.

**Solo se envían las fronteras cuyo dato tuvimos que sustituir.** Si el CGM de
Quoia ya reportó válido por su cuenta (`medidor_usado == 'cgm'`, o `caso ==
'CGM'` en consumo), no se toca: enviar de más sobreescribiría un reporte oficial
que ya estaba bien. `'excluida'` también se salta — su `curva_final` es None
mientras dure la exclusión, y sin ese chequeo se mandaría una curva de 0 kWh
FABRICADA para una frontera que justamente no debe reportar nada.

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
from apps.comun.integraciones.gaia_client import GaiaClient


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


def enviar_background(fecha: date) -> None:
    """`enviar()` en un hilo aparte, con el resultado a la caché. Quien la
    llama ya tomó la marca con `tomar_envio()`; acá solo se libera."""
    close_old_connections()
    inicio = time.monotonic()
    try:
        resultado = enviar(fecha)
        print(
            f"[reporte_energia] enviar_background fecha={fecha} "
            f"enviados={resultado['enviados']} fallidos={len(resultado['fallidos'])} "
            f"bloqueado={resultado['bloqueado']}"
        )
        _cache_escribir("ultimo_envio", fecha, {
            **resultado, "fecha": str(fecha),
            "duracion_s": round(time.monotonic() - inicio, 1),
            "terminado_en": datetime.now(timezone.utc).isoformat(),
        }, _TTL_ULTIMO_ENVIO)
    except Exception:
        print(f"[reporte_energia] enviar_background fecha={fecha} FALLÓ:")
        print(traceback.format_exc())
        _cache_escribir("ultimo_envio", fecha, {
            "fecha": str(fecha),
            "enviados": 0, "fallidos": [], "bloqueado": False,
            "duracion_s": round(time.monotonic() - inicio, 1),
            "terminado_en": datetime.now(timezone.utc).isoformat(),
            "error_general": (
                "El envío se interrumpió. Lo que alcanzó a salir quedó registrado "
                "en cada frontera; ver logs."
            ),
        }, _TTL_ULTIMO_ENVIO)
    finally:
        _cache_borrar("envio_en_curso", fecha)
        close_old_connections()


def resumen_envio(fecha: date) -> dict:
    """Qué pasó con cada frontera del día en el envío: enviada, fallida, sin
    enviar todavía, o que no se envía (automática o excluida).

    Sale de las filas, no de la caché: cada fila se guarda apenas se envía, así
    que contado mientras corre el envío da el avance en vivo, y después sigue
    disponible al volver a abrir el día. Mientras hay un envío en curso, una
    fila con `enviado_quoia_en` de ANTES de que arrancara es de un envío
    anterior: cuenta como "por enviar" hasta que esta corrida llegue a ella.

    "Automáticas" son las que el CGM de Quoia ya reportó bien por su cuenta, y
    "excluidas" las que tienen una exclusión vigente. Las dos salen de
    `reporte_ya_valido`, la misma regla que decide no enviarlas: el conteo
    coincide siempre con lo que de verdad hizo `enviar()`.
    """
    en_curso = envio_en_curso(fecha)
    desde = None
    if en_curso and en_curso.get("desde"):
        desde = datetime.fromisoformat(en_curso["desde"])

    gen_filas, con_filas = _filas_del_dia(fecha)
    conteos = {"enviadas": 0, "fallidas": 0, "por_enviar": 0, "automaticas": 0, "excluidas": 0}
    fallidas: list[dict] = []
    filas = ([(rep, front, "generacion") for rep, front in gen_filas]
             + [(rep, front, "consumo") for rep, front in con_filas])
    for rep, front, tipo in filas:
        if rep.medidor_usado == "excluida":
            conteos["excluidas"] += 1
        elif reporte_ya_valido(rep, tipo == "generacion"):
            conteos["automaticas"] += 1
        elif rep.enviado_quoia_en is None or (desde and rep.enviado_quoia_en < desde):
            conteos["por_enviar"] += 1
        elif rep.enviado_quoia_ok:
            conteos["enviadas"] += 1
        else:
            conteos["fallidas"] += 1
            fallidas.append({
                "frontera_id": front.id,
                "nombre_proyecto": _nombre_frontera(front),
                "tipo": tipo,
                "motivo": rep.enviado_quoia_error,
            })
    return {"total": len(filas), **conteos, "fallidas_detalle": fallidas}
