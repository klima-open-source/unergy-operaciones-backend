"""Puebla los `*_id` de las partes de los contratos que ya existen.

**Por defecto solo lee.** Escribe únicamente con `--aplicar`.

    python manage.py vincular_partes_contratos
    python manage.py vincular_partes_contratos --csv partes.csv
    python manage.py vincular_partes_contratos --aplicar

Para qué. Las SEIS partes que nombra un contrato --contratante, prestador e
inversionista en `contratos_servicio`; comprador y vendedor en `ppa_contratos`;
y el arrendador en `arr_arrendador`-- tienen clave foránea a `clientes`, y la
auditoría del 2026-08-27 encontró 0 de 162 contratos con el vínculo puesto. El
arrendador ni siquiera tenía la columna: nació el 2026-09-18, así que TODAS sus
filas están sin vincular. Eran dos causas: el autocompletado perdía
el id al teclear, y `ContratoEscrituraSerializer` descartaba en silencio la clave
`contratante_id` que el frontend sí mandaba. Las dos están corregidas
(`docs/SERVICIOS_AGRUPACION.md` §4-decies), pero eso solo arregla lo que se
guarde de ahora en adelante: los contratos viejos siguen nombrando a sus partes
por texto.

Mientras queden sin vincular, cada cálculo que necesita saber de quién es un
contrato tiene que adivinar comparando nombres --el reparto de costos por
inversionista lo hace hoy-- y `partes.sincronizar` tiene que seguir resolviendo
al vuelo en cada guardado. Este comando es lo que permite retirar esa adivinanza:
se corre UNA vez, se revisa lo que no emparejó, y el vínculo queda escrito.

Cómo empareja. Reutiliza `partes.emparejar_cliente`, la única definición, y
**solo escribe lo seguro**: NIT exacto, o el mismo nombre (mismas palabras, en
cualquier orden). Lo que solo se PARECE a un cliente sale como "revisar a mano"
con el cliente sugerido, y no se escribe: el 2026-10-07, contra datos de
producción, 11 de 12 parecidos eran OTRA empresa ("Bia Energy" → "BALI ENERGY",
cinco PPA). Y un vínculo malo no se queda quieto: el siguiente guardado del
contrato copia el nombre del cliente encima del de la parte.

Lo que NO hace. No inventa clientes: una parte cuyo nombre no corresponde a
ningún cliente registrado sale en el informe como pendiente, para darla de alta
a mano. Y no toca una parte que ya tenga su vínculo, aunque apunte a otro
cliente que el que el nombre sugiere: corregir un vínculo existente es una
decisión de alguien, no de un emparejamiento por texto.
"""
import csv

from django.core.management.base import BaseCommand

from apps.arriendos.models import ArrArrendador
from apps.clientes.models import Cliente
from apps.contratos.models import ContratoServicio
from apps.contratos.services import partes as partes_service
from apps.facturacion.models import ContratoFactura
from apps.ppa.models import PpaContrato

#: Modelo → los roles cuyo `*_id` hay que poblar, y si el rol tiene columna NIT.
#: `ContratoServicio` no tiene `deleted_at` --se borra de verdad--, así que el
#: filtro de vivos solo aplica al PPA.
OBJETIVOS = (
    (ContratoServicio, "contratos_servicio",
     (("contratante", True), ("prestador", True), ("inversionista", False))),
    (PpaContrato, "ppa_contratos",
     (("comprador", True), ("vendedor", True))),
    # El arrendador es la sexta parte, y su vinculo (`cliente_id`) nacio el
    # 2026-09-18: TODAS sus filas estan sin vincular. Su campo de texto se llama
    # `nombre` a secas, no `<rol>_nombre`, y no tiene NIT.
    (ArrArrendador, "arr_arrendador", (("cliente", False),)),
    # Las facturas de un contrato tambien nombraban a su inversionista con
    # texto; su vinculo nacio el 2026-09-20, asi que tampoco hay ninguna
    # vinculada.
    (ContratoFactura, "contrato_factura", (("inversionista", False),)),
)

#: Rol → el campo de texto del que sale el nombre, cuando no es `<rol>_nombre`.
CAMPO_NOMBRE = {"cliente": "nombre"}


def _vivos(modelo):
    """Las filas a revisar, saltando las borradas donde el modelo lo permita."""
    campos = {f.name for f in modelo._meta.get_fields()}
    consulta = modelo.objects.all()
    return consulta.filter(deleted_at__isnull=True) if "deleted_at" in campos else consulta

CABECERA = ["tabla", "contrato_id", "rol", "nombre", "nit", "resultado", "cliente_id",
            "cliente_nombre"]

#: Lo que se escribe sin que nadie lo mire. El parecido, no.
SEGUROS = (partes_service.POR_NIT, partes_service.POR_NOMBRE)


class Command(BaseCommand):
    help = "Puebla los *_id de las partes de los contratos. Solo lee sin --aplicar."

    def add_arguments(self, parser):
        parser.add_argument(
            "--aplicar", action="store_true",
            help="Escribe los vínculos. Sin esto, solo informa.",
        )
        parser.add_argument(
            "--csv", dest="csv_path", default=None,
            help="Escribe el detalle parte por parte en un CSV, para revisarlo.",
        )

    def handle(self, *args, **opciones):
        aplicar = opciones["aplicar"]
        filas = []
        resumen = {"ya_vinculadas": 0, "resueltas": 0, "a_revisar": 0,
                   "sin_candidato": 0, "sin_nombre": 0}
        nombres = dict(Cliente.objects.values_list("id", "razon_social_nombre"))

        for modelo, tabla, roles in OBJETIVOS:
            for contrato in _vivos(modelo):
                cambios = []
                for rol, tiene_nit in roles:
                    campo = CAMPO_NOMBRE.get(rol, f"{rol}_nombre")
                    nombre = getattr(contrato, campo, None)
                    nit = getattr(contrato, f"{rol}_nit", None) if tiene_nit else None

                    if getattr(contrato, f"{rol}_id", None):
                        resumen["ya_vinculadas"] += 1
                        continue
                    if not (nombre or "").strip():
                        resumen["sin_nombre"] += 1
                        continue

                    cliente_id, como = partes_service.emparejar_cliente(nombre, nit)
                    if como in SEGUROS:
                        resumen["resueltas"] += 1
                        cambios.append((rol, cliente_id))
                        resultado = f"resuelta_por_{como}"
                    elif como == partes_service.PARECIDO:
                        resumen["a_revisar"] += 1
                        resultado = "revisar_a_mano"
                    else:
                        resumen["sin_candidato"] += 1
                        resultado = "sin_candidato"
                    filas.append([
                        tabla, contrato.id, rol, nombre, nit or "", resultado,
                        cliente_id or "", nombres.get(cliente_id, ""),
                    ])

                if cambios and aplicar:
                    for rol, cliente_id in cambios:
                        setattr(contrato, f"{rol}_id", cliente_id)
                    contrato.save(update_fields=[rol for rol, _ in cambios])

        self._informar(resumen, aplicar)
        self._listar(filas)
        if opciones["csv_path"]:
            self._escribir_csv(opciones["csv_path"], filas)

    def _informar(self, resumen, aplicar):
        escribir = self.stdout.write
        escribir("")
        escribir(self.style.MIGRATE_HEADING("Partes de contratos"))
        escribir(f"  Ya vinculadas, sin tocar : {resumen['ya_vinculadas']}")
        escribir(f"  Emparejadas (NIT o mismo nombre): {resumen['resueltas']}")
        escribir(f"  Parecidas, a revisar a mano     : {resumen['a_revisar']}  (no se escriben)")
        escribir(f"  Sin cliente que empareje        : {resumen['sin_candidato']}")
        escribir(f"  Sin nombre que buscar    : {resumen['sin_nombre']}")
        escribir("")
        if aplicar:
            escribir(self.style.SUCCESS(
                f"  Escritas {resumen['resueltas']} vinculaciones."
            ))
        else:
            escribir(self.style.WARNING(
                "  Simulación: no se escribió nada. Repite con --aplicar."
            ))
        if resumen["sin_candidato"]:
            escribir(
                "  Las que no emparejaron necesitan que se dé de alta el cliente,\n"
                "  o que se revise el nombre a mano. Usa --csv para verlas."
            )
        escribir("")

    def _listar(self, filas):
        """Lo que pide a una persona, en la salida: desde el workflow de comandos
        no se puede bajar el CSV del servidor."""
        for resultado, titulo in (
            ("revisar_a_mano", "Parecidas a un cliente: revisar a mano (NO se escriben)"),
            ("sin_candidato", "Sin cliente: darlo de alta o corregir el nombre"),
        ):
            propias = [f for f in filas if f[5] == resultado]
            if not propias:
                continue
            self.stdout.write(self.style.MIGRATE_HEADING(f"{titulo} ({len(propias)})"))
            for tabla, cid, rol, nombre, nit, _r, sugerido_id, sugerido in propias:
                sugerencia = f"  ->  ¿{sugerido} (cliente {sugerido_id})?" if sugerido_id else ""
                self.stdout.write(
                    f"  {tabla} {cid} {rol}: {nombre}{f' (NIT {nit})' if nit else ''}{sugerencia}"
                )
            self.stdout.write("")

    def _escribir_csv(self, ruta, filas):
        with open(ruta, "w", newline="", encoding="utf-8") as f:
            escritor = csv.writer(f)
            escritor.writerow(CABECERA)
            escritor.writerows(filas)
        self.stdout.write(self.style.SUCCESS(f"  CSV escrito en {ruta} ({len(filas)} filas)"))
