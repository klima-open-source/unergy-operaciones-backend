"""Carga los patrimonios autónomos y pasa sus plantas a sus dueños. UNA VEZ.

**Por defecto solo lee.** Escribe únicamente con `--aplicar`.

    python manage.py cargar_patrimonios
    python manage.py cargar_patrimonios --aplicar

Para qué. Hasta el 2026-10-14 el patrimonio estaba cargado COMO participante de la
planta (inversionista al 100 %). Modelo de Sara: el dueño es el inversionista y el
patrimonio es por el que la tiene, a quien se le factura (`patrimonio_id`). Datos
del documento de Sara del mismo día:

- Fiduciaria Bancolombia (general) → 17434 P.A. Valle de Gandalf (Ayura; la parte
  del patrimonio pasa a Ayura con el mismo %), 17844 Sol de la Sierra (Ayura 50 %
  y Solenium 50 % en todas sus plantas), 18255 P.A. Autoconsumo IML y 18254 P.A.
  Autoconsumo Nestlé (sin plantas).
- Fideicomisos BBVA (general) → Ibirico y Mapalé, de Sun Capital.
- Credicorp Capital (general), sin plantas.

**Nada del historial se suelta.** La participación del patrimonio se TRANSFORMA en
la del dueño (conserva su id: paneles, facturas de liquidación, mandatos y
consecutivos siguen apuntando a ella). Si el dueño ya tenía su propia fila en la
planta (Sol de la Sierra: enero–febrero de 2026, antes del patrimonio), sus
referencias pasan a la fila transformada y la sobrante se elimina. Cada
participación de Sol de la Sierra queda desde la primera fecha que tenía la planta
y sin fecha de fin: los dueños siempre fueron Ayura y Solenium (Sara).

Se detiene sin escribir si falta una ficha, una planta no se encuentra o una planta
no termina sumando 100 % entre sus participaciones vigentes.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.clientes.models import Cliente
from apps.proyectos.models import Inversionista, Patrimonio, Proyecto, ProyectoInversionista

# Fichas, por NIT tal como están en `clientes`.
NIT_FIDUCIARIA_BANCOLOMBIA = "830.054.539-2"
NIT_SOL_DE_LA_SIERRA = "830.054.539-1"
NIT_BBVA = "830.052.998-9"
NIT_CREDICORP = "9005312927"
NIT_AYURA = "860.043.345-0"
NIT_SOLENIUM = "901.097.244-5"
NIT_SUN_CAPITAL = "901.605.102-9"

# (clave, nombre, código, general, NIT de su ficha o None)
GENERALES = [
    ("bancolombia", "Fiduciaria Bancolombia", None, None, NIT_FIDUCIARIA_BANCOLOMBIA),
    ("bbva", "Fideicomisos BBVA", None, None, NIT_BBVA),
    ("credicorp", "Credicorp Capital Fiduciaria", None, None, NIT_CREDICORP),
]
SUBPATRIMONIOS = [
    ("gandalf", "P.A. Valle de Gandalf", "17434", "bancolombia", None),
    ("sierra", "Sol de la Sierra", "17844", "bancolombia", NIT_SOL_DE_LA_SIERRA),
    ("iml", "P.A. Autoconsumo IML", "18255", "bancolombia", None),
    ("nestle", "P.A. Autoconsumo Nestlé", "18254", "bancolombia", None),
]

PLANTAS_GANDALF = [
    "Minigranja Solar Uruaco", "MGS 0005 Cañahuate", "MGS 0004 Valle de Gandalf",
    "MGS 0006 Perija", "MGS 0007 La Paz Vallenata", "MGS 0009 El Molino",
    "MGS 0008 La Paz Verso", "MGS 0017- Esmeralda", "MGS 0010 - Villanueva",
    "MGS 0016 - Puya", "MGS 0014 - El Olimpo", "MGS 0013 La Mesa",
]
PLANTAS_SIERRA = [
    "MGS 0018 La Paz Leyenda", "MGS 0024 - San Diego Sur", "MGS 0019 El Merengue",
    "MGS 0026 Valencia Oriente 1", "MGS 0027 Valencia Oriente 2", "MGS 0022 - La Cumbia",
    "MGS 0025 - El Copey Occidente", "MGS 0023 Joropo",
]
PLANTAS_BBVA = ["MGS 0021 Ibirico", "Minigranja 0020 - El Mapalé"]

MITAD = Decimal("0.5")


def _ficha(nit):
    filas = list(Cliente.objects.filter(nit_cedula=nit))
    if len(filas) != 1:
        raise CommandError(f"Se esperaba una ficha con NIT {nit} y hay {len(filas)}.")
    return filas[0]


def _planta(nombre):
    filas = list(Proyecto.objects.filter(nombre_comercial=nombre, deleted_at__isnull=True))
    if len(filas) != 1:
        raise CommandError(f'Se esperaba una planta "{nombre}" y hay {len(filas)}.')
    return filas[0]


def _referencias():
    """Las columnas que apuntan a `proyecto_inversionistas.id`, leídas de la base:
    así no se escapa ninguna aunque mañana aparezca otra."""
    with connection.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT tc.table_name, kcu.column_name
              FROM information_schema.table_constraints tc
              JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = tc.constraint_name
              JOIN information_schema.constraint_column_usage ccu
                ON ccu.constraint_name = tc.constraint_name
             WHERE tc.constraint_type = 'FOREIGN KEY'
               AND ccu.table_name = 'proyecto_inversionistas' AND ccu.column_name = 'id'
        """)
        return sorted(cur.fetchall())


def _mover_referencias(desde_id, hacia_id, columnas) -> dict:
    movidas = {}
    with connection.cursor() as cur:
        for tabla, columna in columnas:
            cur.execute(f'UPDATE "{tabla}" SET "{columna}" = %s WHERE "{columna}" = %s',
                        [hacia_id, desde_id])
            if cur.rowcount:
                movidas[tabla] = cur.rowcount
    return movidas


def _fila(p):
    fin = p.fecha_fin.isoformat() if p.fecha_fin else "sin fin"
    ini = p.fecha_inicio.isoformat() if p.fecha_inicio else "sin inicio"
    pct = f"{p.porcentaje_participacion * 100:.2f} %" if p.porcentaje_participacion is not None else "—"
    pat = f" vía {p.patrimonio.nombre}" if p.patrimonio_id else ""
    return f"#{p.id} {p.inversionista.cliente.razon_social_nombre}: {pct}{pat} ({ini} → {fin})"


class Command(BaseCommand):
    help = "Carga los patrimonios y pasa sus plantas a sus dueños (solo lee sin --aplicar)."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Escribe los cambios.")

    def handle(self, *args, aplicar=False, **opciones):
        self.columnas = _referencias()
        with transaction.atomic():
            patrimonios = self._patrimonios()
            ayura = Inversionista.objects.get_or_create(cliente=_ficha(NIT_AYURA))[0]
            solenium = Inversionista.objects.get_or_create(cliente=_ficha(NIT_SOLENIUM))[0]
            sun_capital = _ficha(NIT_SUN_CAPITAL)
            pa_bancolombia = _ficha(NIT_FIDUCIARIA_BANCOLOMBIA)
            pa_sierra = _ficha(NIT_SOL_DE_LA_SIERRA)

            self.stdout.write("\n== P.A. Valle de Gandalf: la parte del patrimonio pasa a Ayura")
            for nombre in PLANTAS_GANDALF:
                self._gandalf(_planta(nombre), pa_bancolombia, ayura, patrimonios["gandalf"])

            self.stdout.write("\n== Sol de la Sierra: Ayura 50 % y Solenium 50 %")
            for nombre in PLANTAS_SIERRA:
                self._sierra(_planta(nombre), pa_sierra, ayura, solenium, patrimonios["sierra"])

            self.stdout.write("\n== Fideicomisos BBVA: Sun Capital, a través del general")
            for nombre in PLANTAS_BBVA:
                self._bbva(_planta(nombre), sun_capital, patrimonios["bbva"])

            if not aplicar:
                transaction.set_rollback(True)
        self.stdout.write("\n" + ("Aplicado." if aplicar
                                  else "Solo lectura: corre con --aplicar para escribir."))

    # ── los patrimonios ──────────────────────────────────────────────────────

    def _patrimonios(self) -> dict:
        salida = {}
        self.stdout.write("== Patrimonios")
        for clave, nombre, codigo, general, nit in GENERALES + SUBPATRIMONIOS:
            padre = salida.get(general)
            ficha = _ficha(nit) if nit else None
            existente = (Patrimonio.objects.filter(codigo=codigo).first() if codigo
                         else Patrimonio.objects.filter(nombre=nombre, padre__isnull=True).first())
            if existente:
                salida[clave] = existente
                self.stdout.write(f"  ya existe: {existente.nombre}")
                continue
            salida[clave] = Patrimonio.objects.create(
                nombre=nombre, codigo=codigo, padre=padre, cliente=ficha)
            nivel = f"subpatrimonio de {padre.nombre}" if padre else "general"
            self.stdout.write(f"  nuevo: {nombre}{f' ({codigo})' if codigo else ''}, {nivel}"
                              f"{f', ficha {ficha.razon_social_nombre}' if ficha else ''}")
        return salida

    # ── por grupo de plantas ─────────────────────────────────────────────────

    def _antes(self, planta):
        filas = list(planta.inversionistas.select_related("inversionista__cliente", "patrimonio")
                     .order_by("id"))
        self.stdout.write(f"\n  {planta.nombre_comercial}")
        for p in filas:
            self.stdout.write(f"    antes:   {_fila(p)}")
        return filas

    def _despues(self, planta):
        vigentes = Decimal("0")
        for p in (planta.inversionistas.select_related("inversionista__cliente", "patrimonio")
                  .order_by("id")):
            self.stdout.write(f"    después: {_fila(p)}")
            if p.fecha_fin is None:
                vigentes += p.porcentaje_participacion or 0
        if vigentes != 1:
            raise CommandError(f"{planta.nombre_comercial}: las participaciones vigentes suman "
                               f"{vigentes * 100:.4f} %, no 100 %. No se escribe nada.")

    def _gandalf(self, planta, pa, ayura, patrimonio):
        filas = self._antes(planta)
        del_pa = [p for p in filas if p.inversionista.cliente_id == pa.id]
        if not del_pa:
            self.stdout.write("    sin participación del patrimonio: no se toca")
        for p in del_pa:
            if any(o.inversionista_id == ayura.id for o in filas if o.pk != p.pk):
                raise CommandError(f"{planta.nombre_comercial}: Ayura ya tiene otra fila.")
            p.inversionista = ayura
            p.patrimonio = patrimonio
            p.save(update_fields=["inversionista", "patrimonio", "updated_at"])
        self._despues(planta)

    def _sierra(self, planta, pa, ayura, solenium, patrimonio):
        filas = self._antes(planta)
        inicios = [p.fecha_inicio for p in filas]
        inicio = None if (not inicios or None in inicios) else min(inicios)
        del_pa = [p for p in filas if p.inversionista.cliente_id == pa.id]
        if len(del_pa) > 1:
            raise CommandError(f"{planta.nombre_comercial}: el patrimonio tiene dos filas.")
        base = del_pa[0] if del_pa else None

        for dueno in (ayura, solenium):
            propia = next((p for p in filas if p.inversionista_id == dueno.id), None)
            if base is not None:
                # La fila del patrimonio se queda con el primer dueño; la propia de ese
                # dueño le pasa sus referencias y sobra.
                if propia is not None:
                    movidas = _mover_referencias(propia.id, base.id, self.columnas)
                    self.stdout.write(f"    #{propia.id} → #{base.id}: {movidas or 'nada que mover'}"
                                      f"; se elimina #{propia.id}")
                    propia.delete()
                fila, base = base, None
                fila.inversionista = dueno
            elif propia is not None:
                fila = propia
            else:
                fila = ProyectoInversionista(proyecto=planta, inversionista=dueno)
            fila.porcentaje_participacion = MITAD
            fila.patrimonio = patrimonio
            fila.fecha_inicio = inicio
            fila.fecha_fin = None
            fila.save()
        self._despues(planta)

    def _bbva(self, planta, sun_capital, patrimonio):
        filas = self._antes(planta)
        for p in filas:
            if p.inversionista.cliente_id == sun_capital.id:
                p.patrimonio = patrimonio
                p.save(update_fields=["patrimonio", "updated_at"])
        self._despues(planta)
