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
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from apps.energia.models import ReporteEnergiaConsumo, ReporteEnergiaGeneracion
from apps.energia.services.reporte.borders import resolver_borders
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


def enviar(fecha: date) -> dict:
    """Envía el reporte del día a Quoia -- bloqueado si queda alguna
    frontera con 'Revisar Manualmente' pendiente (huecos sin fuente).

    Solo se envían las fronteras donde tuvimos que sustituir el dato de
    Quoia (medidor_usado != 'cgm' / caso != 'CGM') -- si el CGM de Quoia ya
    reportó válido por su cuenta, no se toca.
    """
    hay_pendientes = (
        ReporteEnergiaGeneracion.objects.filter(
            fecha=fecha, revisar_manualmente=True).exists()
        or ReporteEnergiaConsumo.objects.filter(
            fecha=fecha, revisar_manualmente=True).exists()
    )
    if hay_pendientes:
        return {
            "fecha": fecha, "enviados": 0, "fallidos": [], "bloqueado": True,
            "motivo_bloqueo": (
                "Quedan fronteras con horas sin fuente (Revisar Manualmente) sin validar."
            ),
        }

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

    frt_codes = {f.codigo_frontera for _, f in gen_filas + con_filas if f.codigo_frontera}
    gaia = GaiaClient()
    borders = resolver_borders(gaia, frt_codes) if frt_codes else {}

    enviados = 0
    fallidos: list[str] = []

    def _procesar(rep, front, es_generacion: bool) -> None:
        nonlocal enviados
        resultado, motivo = _enviar_a_quoia(rep, front, es_generacion, gaia, borders)
        if resultado is True:
            enviados += 1
        elif resultado is False:
            fallidos.append(f"{_nombre_frontera(front)} — {motivo}")
        # resultado is None: ya era válido en Quoia, no hacía falta nada

    for rep, front in gen_filas:
        _procesar(rep, front, es_generacion=True)
    for rep, front in con_filas:
        _procesar(rep, front, es_generacion=False)

    # Se guarda el resultado del envío de CADA fila, incluidas las que
    # fallaron: `enviado_quoia_error` es lo que después explica el fallo.
    CAMPOS = ["enviado_quoia_en", "enviado_quoia_ok", "enviado_quoia_error"]
    for filas, Modelo in ((gen_filas, ReporteEnergiaGeneracion),
                          (con_filas, ReporteEnergiaConsumo)):
        tocadas = [rep for rep, _ in filas if rep.enviado_quoia_en is not None]
        if tocadas:
            Modelo.objects.bulk_update(tocadas, CAMPOS)

    return {
        "fecha": fecha, "enviados": enviados, "fallidos": fallidos, "bloqueado": False,
    }


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


def estado_quoia_revisar(fecha: date) -> dict:
    """Consulta Quoia para las filas enviadas que aún no tienen respuesta.

    Solo vuelve a golpear Quoia para las que están en espera (`xm_exitoso is
    None`): está pensado para dispararse justo después de `/enviar` y llamarse
    cada tanto hasta que nadie quede en espera.
    """
    filas = _fronteras_enviadas(fecha)
    pendientes = [f for f in filas if f[0].xm_exitoso is None]

    if pendientes:
        gaia = GaiaClient()
        frt_codes = {f.codigo_frontera for _, f, _ in pendientes if f.codigo_frontera}
        borders = resolver_borders(gaia, frt_codes) if frt_codes else {}
        ahora = datetime.now(timezone.utc)
        por_modelo: dict = {}
        for rep, front, tipo in pendientes:
            meta = borders.get((front.codigo_frontera or "").strip().lower())
            border_id = meta.get("id") if meta else None
            estado = gaia.get_border_report_status(border_id, str(fecha)) if border_id else None
            rep.xm_verificado_en = ahora
            if estado:
                rep.xm_process_id = estado.get("xm_process_id")
                rep.xm_estado = estado.get("status")
                rep.xm_exitoso = estado.get("success")
            Modelo = (
                ReporteEnergiaGeneracion if tipo == "generacion"
                else ReporteEnergiaConsumo
            )
            por_modelo.setdefault(Modelo, []).append(rep)

        campos = ["xm_verificado_en", "xm_process_id", "xm_estado", "xm_exitoso"]
        for Modelo, reps in por_modelo.items():
            Modelo.objects.bulk_update(reps, campos)

    conteos, fallidas = _conteos(filas)
    return {"fecha": fecha, "total": len(filas), "fallidas": fallidas, **conteos}
