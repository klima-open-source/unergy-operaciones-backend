"""Alertas de aniversario de los contratos de Representación/CGM.

Puerto de `_scheduled_representacion_alertas`, que vivía escrito entero dentro
del registro del scheduler en `app/main.py`. Avisa 30 y 15 días antes del
aniversario de la firma, con la tarifa ya indexada por IPC.

**La fuente es la tabla, no un JSON.** Antes leía `data/DataCGM.json`; ahora sale
de `contratos_servicio`, que es lo que la plataforma muestra y deja editar, así
que un cambio de tarifa o de fecha de firma hecho en la UI se refleja en la
alerta.

**El IPC también sale de la tabla.** Antes eran tres tasas en un dict con un
5,1% por defecto para todo lo demás, así que un aniversario de 2027 se indexaba
a un número inventado sin decirlo. Ahora sale de `om_ipc_tasas`, filtrando
`confirmado=True`, y lo que no está confirmado se reporta como pendiente en vez
de rellenarse. Ver `tasas_ipc_confirmadas` e `ipc_de` para por qué la búsqueda
va por el año del aniversario pero el correo muestra el anterior.

Los destinatarios son los mismos que los de vencimiento de PPA, a propósito
(confirmado con negocio el 2026-08-25): `ppa.services.vencimientos.correos_de_alerta`.

Las dos filas de tarifa se arman en Python y entran como un solo marcador. En
FastAPI la plantilla era una f-string con condicionales adentro de las llaves
—`{'<tr>…' if valor else ""}`— y eso no sobrevive a un `.format()`; separarlo
además deja el HTML legible.
"""

from __future__ import annotations

import logging
import os
from datetime import date

from apps.contratos.models import ContratoServicio
from apps.om.models import OmIpcTasa
from apps.plataforma.services.fechas import hoy_col
from apps.ppa.services.vencimientos import correos_de_alerta

logger = logging.getLogger("operaciones.contratos.alertas")

# Días antes del aniversario en que se avisa.
AVISOS = (30, 15)

FILA_TARIFA = (
    '<tr><td style="padding:6px 0;color:#6B5F80">Nueva tarifa {etiqueta}</td>'
    '<td style="padding:6px 0;font-weight:600;color:{color}">{valor} $/kWh</td></tr>'
)

# Se muestra cuando la tarifa se pudo calcular pero algún aniversario viejo cayó
# en un año sin tasa: el número queda por debajo del real y hay que decirlo.
NOTA_PARCIAL = (
    '<tr><td style="padding:6px 0;color:#6B5F80">Proyección</td>'
    '<td style="padding:6px 0;font-weight:600;color:#b45309">'
    'parcial — sin IPC confirmado de {anios}</td></tr>'
)

PLANTILLA = """
<html>
<body style="font-family:Arial,sans-serif;color:#1A0F2E;max-width:560px;margin:0 auto;padding:0">
  <div style="background:#1A0F2E;padding:24px 28px;border-radius:10px 10px 0 0">
    <div style="color:#F6FF72;font-size:20px;font-weight:800;letter-spacing:1px">UNERGY</div>
    <div style="color:#6B5F80;font-size:11px;letter-spacing:.8px;text-transform:uppercase;margin-top:2px">
      Alerta de Renovacion CGM
    </div>
  </div>
  <div style="background:#F7F4FD;padding:28px;border:1px solid #EDE8F5;border-top:none;border-radius:0 0 10px 10px">
    <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:8px;padding:12px 16px;margin-bottom:20px">
      <strong>En {dias} dias</strong> se cumple el aniversario del contrato
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:14px">
      <tr><td style="padding:6px 0;color:#6B5F80;width:180px">Proyecto</td>
          <td style="padding:6px 0;font-weight:600">{proyecto}</td></tr>
      <tr><td style="padding:6px 0;color:#6B5F80">Inversionista</td>
          <td style="padding:6px 0;font-weight:600">{inversionista}</td></tr>
      <tr><td style="padding:6px 0;color:#6B5F80">Fecha aniversario</td>
          <td style="padding:6px 0;font-weight:600">{aniversario}</td></tr>
      <tr><td style="padding:6px 0;color:#6B5F80">IPC aplicado</td>
          <td style="padding:6px 0;font-weight:600">{ipc_texto}</td></tr>
      {filas_tarifa}
      {nota_parcial}
    </table>
    <p style="color:#6B5F80;font-size:12px;margin-top:20px">
      Este es un mensaje automatico del sistema de Operaciones Unergy.<br>
      <a href="mailto:operaciones@unergy.io" style="color:#915BD8">operaciones@unergy.io</a>
    </p>
  </div>
</body>
</html>"""


def proximo_aniversario(firma: date, hoy: date) -> tuple[date, int] | None:
    """La fecha del próximo aniversario y cuál número es, o None si no hay.

    Mira hasta diez años hacia adelante. El 29 de febrero cae al 28 en los años
    no bisiestos: es la única fecha de firma que no existe todos los años.
    """
    for numero in range(1, 10):
        anio = firma.year + numero
        try:
            aniversario = date(anio, firma.month, firma.day)
        except ValueError:
            aniversario = date(anio, firma.month, 28)
        if aniversario >= hoy:
            return aniversario, numero
    return None


def tasas_ipc_confirmadas() -> dict[int, float]:
    """Las tasas de IPC que alguien ya confirmó, por año de APLICACIÓN.

    Sale de `om_ipc_tasas` —la única de las dos tablas de IPC con mantenimiento:
    `om.revisar_ipc_del_anio` le crea la fila cada 1-enero y hay pantalla para
    confirmarla— y no de un dict escrito a mano, que se quedaba viejo en silencio.

    `confirmado=True` no es opcional: la tarea del 1-enero deja la fila del año
    nuevo en `tasa=0.0, confirmado=False` esperando el dato del DANE, y leerla
    sin filtrar aplicaría 0% de indexación sin que nada lo dijera.
    """
    return {fila.año: float(fila.tasa)
            for fila in OmIpcTasa.objects.filter(confirmado=True)}


def ipc_de(anio_aniversario: int, tasas: dict[int, float]) -> tuple[float | None, int]:
    """`(tasa, año que se muestra)` para ese aniversario. `None` si nadie la confirmó.

    La búsqueda es por el año del aniversario, directo, porque la tabla indexa por
    año de aplicación. El año que se MUESTRA es el anterior: el número es el IPC de
    dic-2025 aunque sea la fila 2026 la que indexa el aniversario de 2026.
    """
    return tasas.get(anio_aniversario), anio_aniversario - 1


def factor_ipc(anio_aniversario: int, numero: int,
               tasas: dict[int, float]) -> tuple[float, list[int]]:
    """`(factor acumulado, años sin tasa)` de todos los aniversarios cumplidos.

    Es el producto de `(1 + tasa)` año por año, no una sola tasa elevada a
    `numero`: los años reales fueron distintos (9,28% en 2024, 5,20% en 2025,
    5,10% en 2026) y capitalizar el último tres veces da otro número. Mismo
    criterio que `factor_acumulado` de O&M.

    Los años sin tasa se saltan y se devuelven aparte, para poder avisar que la
    proyección quedó corta en vez de inventarles un valor.
    """
    factor = 1.0
    faltantes: list[int] = []
    for anio in range(anio_aniversario - numero + 1, anio_aniversario + 1):
        tasa = tasas.get(anio)
        if tasa is None:
            faltantes.append(anio)
        else:
            factor *= 1 + tasa
    return factor, faltantes


def tarifa_indexada(tarifa: float | None, anio_aniversario: int, numero: int,
                    tasas: dict[int, float]) -> float | None:
    """La tarifa indexada al aniversario, o `None` si no se puede calcular.

    Sin la tasa del propio aniversario no hay número que dar: la tarifa nueva es
    justamente la que ese aniversario estrena, así que conocer las de años
    anteriores no alcanza.
    """
    if not tarifa or tasas.get(anio_aniversario) is None:
        return None
    factor, _ = factor_ipc(anio_aniversario, numero, tasas)
    return round(tarifa * factor, 4)


def contratos_de_representacion() -> list[dict]:
    """Los contratos con servicio de representación y fecha de firma."""
    filas = ContratoServicio.objects.filter(
        servicio_aplica="representacion", fecha_firma_contrato__isnull=False,
    ).select_related("proyecto")
    return [{
        "firma": r.fecha_firma_contrato,
        "proyecto": (r.nombre_proyecto_ref
                     or (r.proyecto.nombre_comercial if r.proyecto else "")).strip(),
        "inversionista": (r.inversionista_nombre or "").strip(),
        "tarifa_cgm": float(r.tarifa_cgm) if r.tarifa_cgm is not None else 0,
        "tarifa_representacion": (float(r.tarifa_representacion)
                                  if r.tarifa_representacion is not None else 0),
    } for r in filas]


def construir_html(contrato: dict, aniversario: date, numero: int, dias: int,
                   tasas: dict[int, float]) -> str:
    tasa, anio_ipc = ipc_de(aniversario.year, tasas)
    _, faltantes = factor_ipc(aniversario.year, numero, tasas)
    nueva_cgm = tarifa_indexada(contrato["tarifa_cgm"], aniversario.year, numero, tasas)
    nueva_rep = tarifa_indexada(contrato["tarifa_representacion"],
                                aniversario.year, numero, tasas)

    filas = ""
    if nueva_cgm:
        filas += FILA_TARIFA.format(etiqueta="CGM", color="#f59e0b", valor=nueva_cgm)
    if nueva_rep:
        filas += FILA_TARIFA.format(etiqueta="Rep.", color="#3b82f6", valor=nueva_rep)

    # Sin tasa la alerta sale igual: avisar del aniversario es el objetivo, y la
    # tarifa es lo secundario. Decir "pendiente" es lo que antes se tapaba con el
    # 5,1% por defecto.
    ipc_texto = (f"{tasa * 100:.2f}% (IPC dic {anio_ipc})" if tasa is not None
                 else f"pendiente de confirmación (IPC dic {anio_ipc})")

    return PLANTILLA.format(
        dias=dias,
        proyecto=contrato["proyecto"],
        inversionista=contrato["inversionista"] or "—",
        aniversario=aniversario.strftime("%d/%m/%Y"),
        ipc_texto=ipc_texto,
        filas_tarifa=filas,
        nota_parcial=(NOTA_PARCIAL.format(anios=", ".join(str(a) for a in faltantes))
                      if filas and faltantes else ""),
    )


def revisar_aniversarios() -> int:
    """Manda las alertas que correspondan hoy. Devuelve cuántas salieron."""
    if not os.environ.get("SMTP_HOST"):
        logger.info("SMTP sin configurar — alertas de representación omitidas")
        return 0

    hoy = hoy_col()
    enviadas = 0
    # Una sola consulta para toda la corrida, no una por contrato.
    tasas = tasas_ipc_confirmadas()

    for contrato in contratos_de_representacion():
        if not contrato["firma"] or not contrato["proyecto"]:
            continue
        proximo = proximo_aniversario(contrato["firma"], hoy)
        if proximo is None:
            continue
        aniversario, numero = proximo
        dias = (aniversario - hoy).days
        if dias not in AVISOS:
            continue
        if _enviar(contrato, aniversario, numero, dias, tasas):
            enviadas += 1

    if enviadas:
        logger.info("alertas de renovación CGM enviadas: %d", enviadas)
    return enviadas


def _enviar(contrato: dict, aniversario: date, numero: int, dias: int,
            tasas: dict[int, float]) -> bool:
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    from app.services.email_service import _log_envio, _smtp_send

    destinatarios = correos_de_alerta()
    if not destinatarios:
        return False

    proyecto = contrato["proyecto"]
    asunto = f"Alerta de renovacion CGM — {proyecto} — {dias} dias para aniversario"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"] = os.environ.get("SMTP_FROM", "operaciones@unergy.io")
    msg["To"] = ", ".join(destinatarios)
    msg.attach(MIMEText(construir_html(contrato, aniversario, numero, dias, tasas),
                        "html", "utf-8"))

    filas = [{"email": e, "tipo": "to"} for e in destinatarios]
    try:
        _smtp_send(msg, destinatarios)
        _log_envio(destinatarios=filas, subject=asunto, tipo="alerta_cgm", success=True)
        return True
    except Exception as exc:
        _log_envio(destinatarios=filas, subject=asunto, tipo="alerta_cgm",
                   success=False, error_msg=str(exc))
        logger.warning("no se pudo enviar la alerta de %s: %s", proyecto, exc)
        return False
