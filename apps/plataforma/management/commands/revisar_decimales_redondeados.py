"""Lista los valores que pudieron guardarse redondeados por el front. SOLO LEE.

    python manage.py revisar_decimales_redondeados
    python manage.py revisar_decimales_redondeados --desde 2026-09-28 --csv sospechosos.csv

Qué pasó. El campo de número de la librería nueva del front (`NumberField` de
Reka UI) ajusta por defecto lo escrito a pasos de 1: un 6,5 se guardaba como 7. Las
pantallas que pasaron a ese campo el 2026-09-28 guardaron así sus decimales hasta
que se le puso `:step-snapping="false"` (front, rama `fix/numberfield-decimales`).
La lista completa de pantallas y columnas está en
`docs/REVISION_DECIMALES_REDONDEADOS.md`.

Qué hace. Un valor redondeado no se distingue de uno que de verdad era entero, así
que esto no corrige nada: lista los **sospechosos** —filas modificadas desde
`--desde` cuyo valor en una de esas columnas es entero— para que una persona los
compare con el documento de origen (la oferta, la factura, el reporte de XM). Las
tablas sin fecha propia usan la de su padre (el contrato, la versión, el registro).

No revisa lo que solo vivió en el navegador o en el JSON `snapshot` de garantías:
eso va en el documento.
"""
import csv
from datetime import date

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db.models import F, Q

#: (modelo, columnas, ruta a la fecha de la fila, descripción para el informe)
REVISAR = (
    ("clientes.Cliente", ("iva_pct", "retencion_pct", "reteiva_pct", "reteica_pct"),
     "updated_at", "Clientes: IVA y retenciones generales"),
    ("clientes.ClienteTasaServicio", ("iva_pct", "retencion_pct", "reteiva_pct", "reteica_pct"),
     "updated_at", "Clientes: IVA y retenciones por servicio"),
    ("contratos.Servicio", ("tarifa_base",), "updated_at",
     "PPA firmados desde el CRM: tarifa base (solo servicios de compra o venta)"),
    ("contratos.Tarifa", ("valor",), "servicio__contrato__updated_at",
     "PPA: tarifas mes a mes (fecha del contrato)"),
    ("comercial.OportunidadOfertaVersionPrecio", ("precio",), "version__updated_at",
     "Ofertas: precio por año de cada versión (fecha de la versión)"),
    ("monitoreo.Falla", ("kwh_perdidos_estimado",), "updated_at", "Fallas: kWh perdidos estimados"),
    ("proyectos.ProyectoInversor", ("potencia_nominal_kw",), "updated_at",
     "Proyectos: potencia nominal de inversores"),
    ("garantias.GarantiaAjuste",
     ("pb", "restricciones", "stn", "trm", "ptb", "total_ungc", "total_ungg", "total_consignar",
      "disponible_custodia", "congelado", "saldo", "total_ajuste_txr"),
     "updated_at", "Garantías: histórico de ajustes"),
    ("liquidaciones.Liquidacion", ("tasa_cambio",), "updated_at", "Liquidaciones: tasa de cambio"),
    ("mercado_xm.AsicSolicitud", ("porcentaje_fncer", "porcentaje_despacho"), "updated_at",
     "GESCON: % FNCER y % despacho"),
    ("registros_cnd.RegistroParametros93",
     ("voltaje_max_kv", "voltaje_nominal_kv", "voltaje_min_kv", "in_eq_ka", "icc_subtrans_pico_kap",
      "icc_subtrans_3f_ka", "icc_subtrans_2f_ka", "icc_subtrans_1f_ka", "icc_estado_estable_ka",
      "impedancia_equivalente_ohm", "frecuencia_max_hz", "frecuencia_min_hz"),
     "registro__updated_at", "Registros CND: parámetros 9.3 (fecha del registro)"),
    ("retos.RetoMetrica", ("meta",), "updated_at", "Retos: meta de cada métrica"),
    ("retos.RetoValorSemanal", ("valor",), "updated_at", "Retos: valores semanales"),
)

#: Valores enteros que de verdad suelen serlo y no vale la pena listar. Por columna:
#: 100 % de FNCER o un despacho de 1,00 (= 100 %) son lo normal.
NO_SOSPECHOSOS = {"*": {0}, "porcentaje_fncer": {0, 100}, "porcentaje_despacho": {0, 1}}


def _es_entero(valor) -> bool:
    try:
        return valor is not None and float(valor) == int(float(valor))
    except (TypeError, ValueError, OverflowError):
        return False


class Command(BaseCommand):
    help = "Lista valores que pudieron guardarse redondeados por el front (solo lee)."

    def add_arguments(self, parser):
        parser.add_argument("--desde", default="2026-09-28",
                            help="Fecha desde la que una fila pudo guardarse redondeada (AAAA-MM-DD).")
        parser.add_argument("--csv", dest="csv_path", default=None,
                            help="Escribe el detalle fila por fila en este CSV.")

    def handle(self, *args, desde, csv_path, **opts):
        desde = date.fromisoformat(desde)
        out = self.stdout.write
        filas_csv = []
        total = 0
        out(f"Valores enteros en columnas de decimales, en filas modificadas desde {desde}")
        out("(son SOSPECHOSOS, no errores: compáralos con el documento de origen)\n")

        for etiqueta, columnas, fecha, descripcion in REVISAR:
            modelo = apps.get_model(etiqueta)
            consulta = modelo.objects.filter(**{f"{fecha}__date__gte": desde})
            if etiqueta == "contratos.Servicio":
                consulta = consulta.filter(servicio__in=("compra", "venta"))
            if etiqueta == "contratos.Tarifa":
                consulta = consulta.filter(mes__isnull=False)
            columnas_con_dato = Q()
            for c in columnas:
                columnas_con_dato |= Q(**{f"{c}__isnull": False})
            consulta = consulta.filter(columnas_con_dato).annotate(_fecha=F(fecha))
            sospechosos = []
            for fila in consulta.values("pk", "_fecha", *columnas):
                for c in columnas:
                    v = fila[c]
                    legitimos = NO_SOSPECHOSOS.get(c, NO_SOSPECHOSOS["*"])
                    if _es_entero(v) and float(v) not in legitimos:
                        sospechosos.append((fila["pk"], c, v, fila["_fecha"]))
            total += len(sospechosos)
            out(f"{len(sospechosos):5d}  {descripcion}  [{modelo._meta.db_table}]")
            # Agrupado por columna y valor: 140 tarifas de 338 son un solo caso a revisar.
            grupos = {}
            for pk, c, v, f in sospechosos:
                grupos.setdefault((c, float(v)), []).append(pk)
            for (c, v), ids in sorted(grupos.items(), key=lambda g: -len(g[1]))[:12]:
                muestra = ", ".join(str(i) for i in ids[:6]) + (" …" if len(ids) > 6 else "")
                out(f"         {c} = {v:.12g}  en {len(ids)} fila(s)  (ids {muestra})")
            if len(grupos) > 12:
                out(f"         … y {len(grupos) - 12} valores más (usa --csv)")
            filas_csv += [(modelo._meta.db_table, pk, c, v, f"{f:%Y-%m-%d}") for pk, c, v, f in sospechosos]

        out(f"\nTotal de valores sospechosos: {total}")
        if csv_path:
            with open(csv_path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["tabla", "id", "columna", "valor", "modificado"])
                w.writerows(filas_csv)
            out(f"Detalle en {csv_path}")
