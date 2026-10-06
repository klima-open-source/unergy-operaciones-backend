"""Copia las dos tablas de contratos a la tabla única (plan 08, entre el deploy 1 y el 2).

    docker compose exec operaciones python manage.py backfill_contratos_unificados --dry-run
    docker compose exec operaciones python manage.py backfill_contratos_unificados
    docker compose exec operaciones python manage.py backfill_contratos_unificados --reset

Qué hace (`docs/refactor/08-plan-django-contratos.md`, §5 "Copia"):

1. `ppa_contratos` → `contratos`, **con el mismo id** (decisión 1) y `grupo = 'ppa'`.
2. `contratos_servicio` → `contratos`, con id nuevo por encima de todos los existentes
   (los ids 1-36 chocan con los de PPA). El par viejo→nuevo queda en
   `contratos_servicio_correspondencia`, que usa la migración del corte para reescribir
   las FK de los satélites (decisión 3).
3. `ppa_contrato_proyectos` → `contrato_proyectos` (mismos ids de PPA: se copia tal cual).
4. `servicios`, una fila por servicio que cubre cada contrato:
   - PPA: `compra` o `venta`, de `tipo_contrato` (nulo = venta, como
     `grupos.subservicio_de_ppa`).
   - Operación: el valor de `servicio_aplica`, que siempre es uno.
   - Representación/CGM: de las tarifas cargadas, como `grupos.subservicios_de` —
     es el ÚNICO dato que dice si un contrato también cubre CGM. Si no tiene ninguna,
     cae a `servicio_aplica`. Esta deducción se usa una sola vez, aquí; después del
     corte `servicios` es la fuente.

Las tablas viejas no se tocan: siguen siendo las que usa la app hasta el deploy 2.

**Ninguna columna se pierde en silencio.** Se copian por nombre todas las columnas que
existen en la tabla vieja y en `contratos`. Una columna vieja sin destino detiene el
comando, salvo las de `COLUMNAS_MUERTAS`, que además tienen que estar vacías.

`--dry-run` hace todo dentro de una transacción y la deshace al final. `--reset` vacía
las tablas nuevas antes (para repetir la copia justo antes del corte). Solo Postgres.

Se BORRA en el deploy 3, con las tablas viejas (`CLAUDE.md`: un backfill se corre una vez
y se borra).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.contratos.services import grupos

#: Columnas de las tablas viejas que no pasan a `contratos`. Ningún modelo ni código las
#: usa desde mayo de 2026 y estaban vacías en los 35 PPA de la copia local al 2026-10-01;
#: el comando lo vuelve a comprobar en la base donde corre.
COLUMNAS_MUERTAS = {
    "ppa_contratos": {"contraparte_nombre", "contraparte_nit", "gescon_codigos_sic"},
    "contratos_servicio": set(),
}

#: Columnas obligatorias de `contratos` que un PPA no trae (la tabla vieja no las tiene).
FIJAS_PPA = {"grupo": f"'{grupos.PPA}'", "estado": "'firmado'", "responsable_iva": "false"}

#: Si una columna obligatoria llega en NULL desde la tabla vieja, este valor (el default
#: del modelo). `created_at`/`updated_at` no van: las dos tablas viejas los traen.
DEFAULT_SI_NULO = {"renovacion_automatica": "false", "responsable_iva": "false",
                   "estado": "'firmado'"}

TABLAS_NUEVAS = ("servicios", "contrato_proyectos", "contratos_servicio_correspondencia",
                 "contratos")


def plan_de_columnas(origen: set[str], destino: set[str], muertas: set[str]) -> list[str]:
    """Las columnas que se copian por nombre. Levanta si alguna del origen no tiene destino.

    Puro (sin base) para poder probarlo. `id` no entra: lo decide cada paso."""
    sin_destino = origen - destino - muertas - {"id"}
    if sin_destino:
        raise CommandError(
            "Columnas de la tabla vieja sin destino en `contratos` (se perderían): "
            + ", ".join(sorted(sin_destino))
        )
    return sorted((origen & destino) - {"id"})


def _grupo_de_servicio_sql() -> str:
    """`servicio_aplica` → grupo, con el catálogo de `grupos` (no hay dos listas)."""
    casos = " ".join(
        f"WHEN '{sub}' THEN '{grupo}'" for sub, grupo in grupos.GRUPO_DE_SUBSERVICIO.items()
        if grupo != grupos.PPA
    )
    return f"CASE servicio_aplica {casos} END"


class _Simulacro(Exception):
    """Señal interna para deshacer la transacción en --dry-run."""


class Command(BaseCommand):
    help = "Copia ppa_contratos y contratos_servicio a la tabla única (plan 08)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Hace todo y lo deshace al final.")
        parser.add_argument("--reset", action="store_true",
                            help="Vacía las tablas nuevas antes de copiar.")

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            raise CommandError("Solo corre en Postgres.")
        try:
            with transaction.atomic():
                self._copiar(reset=opts["reset"])
                self._reportar()
                if opts["dry_run"]:
                    raise _Simulacro
        except _Simulacro:
            self.stdout.write(self.style.WARNING("\n--dry-run: todo se deshizo.\n"))
            return
        self.stdout.write(self.style.SUCCESS("\nCopia hecha.\n"))

    # ── copia ────────────────────────────────────────────────────────────────

    def _copiar(self, *, reset: bool) -> None:
        with connection.cursor() as cur:
            self.cur = cur
            if reset:
                for tabla in TABLAS_NUEVAS:
                    cur.execute(f"DELETE FROM {tabla}")
            elif self._uno("SELECT count(*) FROM contratos"):
                raise CommandError("`contratos` ya tiene filas: usa --reset para repetir la copia.")

            destino = self._columnas("contratos")
            self._exigir_vacias("ppa_contratos")
            self._exigir_vacias("contratos_servicio")
            self._exigir_catalogo()
            self._copiar_ppa(destino)
            self._copiar_servicio(destino)
            cur.execute(
                "INSERT INTO contrato_proyectos (contrato_id, proyecto_id) "
                "SELECT contrato_id, proyecto_id FROM ppa_contrato_proyectos"
            )
            self._llenar_servicios()
            # La secuencia sigue después del mayor id, para los contratos que se creen
            # desde el corte.
            cur.execute(
                "SELECT setval(pg_get_serial_sequence('contratos', 'id'), "
                "(SELECT coalesce(max(id), 1) FROM contratos))"
            )

    def _copiar_ppa(self, destino: set[str]) -> None:
        cols = plan_de_columnas(self._columnas("ppa_contratos"), destino,
                                COLUMNAS_MUERTAS["ppa_contratos"])
        cols = [c for c in cols if c not in FIJAS_PPA]
        self.cur.execute(
            f"INSERT INTO contratos (id, {', '.join(list(FIJAS_PPA) + cols)}) "
            f"SELECT id, {', '.join(list(FIJAS_PPA.values()) + [self._valor(c) for c in cols])} "
            "FROM ppa_contratos"
        )

    def _copiar_servicio(self, destino: set[str]) -> None:
        cols = plan_de_columnas(self._columnas("contratos_servicio"), destino,
                                COLUMNAS_MUERTAS["contratos_servicio"])
        base = self._uno(
            "SELECT greatest((SELECT coalesce(max(id), 0) FROM ppa_contratos), "
            "(SELECT coalesce(max(id), 0) FROM contratos_servicio))"
        )
        # Id nuevo determinista: base + posición por id viejo. El mismo contrato recibe
        # el mismo id en el simulacro y en la copia real. Primero el contrato, después su
        # correspondencia (que lo referencia).
        nuevo_id = f"{base} + row_number() OVER (ORDER BY cs.id)"
        self.cur.execute(
            f"INSERT INTO contratos (id, grupo, {', '.join(cols)}) "
            f"SELECT {nuevo_id}, {_grupo_de_servicio_sql()}, "
            f"{', '.join(self._valor(c, 'cs') for c in cols)} "
            "FROM contratos_servicio cs"
        )
        self.cur.execute(
            "INSERT INTO contratos_servicio_correspondencia (id_viejo, contrato_id) "
            f"SELECT cs.id, {nuevo_id} FROM contratos_servicio cs"
        )

    def _llenar_servicios(self) -> None:
        ahora = "now()"
        insertar = "INSERT INTO servicios (contrato_id, servicio, created_at, updated_at) "
        # PPA: compra o venta (nulo = venta).
        self.cur.execute(
            insertar + f"SELECT id, CASE WHEN tipo_contrato = '{grupos.COMPRA}' "
            f"THEN '{grupos.COMPRA}' ELSE '{grupos.VENTA}' END, {ahora}, {ahora} "
            f"FROM contratos WHERE grupo = '{grupos.PPA}'"
        )
        # Operación: uno solo, el de servicio_aplica.
        self.cur.execute(
            insertar + f"SELECT id, servicio_aplica, {ahora}, {ahora} "
            f"FROM contratos WHERE grupo = '{grupos.OPERACION}'"
        )
        # Representación/CGM: por las tarifas cargadas; sin ninguna, servicio_aplica.
        rc = f"FROM contratos WHERE grupo = '{grupos.REPRESENTACION_CGM}'"
        for sub in grupos.SUBSERVICIOS[grupos.REPRESENTACION_CGM]:
            col = grupos.COLUMNA_TARIFA[sub]
            self.cur.execute(
                insertar + f"SELECT id, '{sub}', {ahora}, {ahora} {rc} AND {col} IS NOT NULL"
            )
        sin_tarifas = " AND ".join(
            f"{grupos.COLUMNA_TARIFA[s]} IS NULL" for s in grupos.SUBSERVICIOS[grupos.REPRESENTACION_CGM]
        )
        self.cur.execute(
            insertar + f"SELECT id, servicio_aplica, {ahora}, {ahora} {rc} AND {sin_tarifas}"
        )

    # ── reporte ──────────────────────────────────────────────────────────────

    def _reportar(self) -> None:
        with connection.cursor() as cur:
            self.cur = cur
            w = self.stdout.write
            w("\n== Conteos ==")
            for tabla in ("ppa_contratos", "contratos_servicio", "ppa_contrato_proyectos"):
                w(f"  {tabla:<36} {self._uno(f'SELECT count(*) FROM {tabla}')}")
            for tabla in ("contratos", "contrato_proyectos", "servicios",
                          "contratos_servicio_correspondencia"):
                w(f"  {tabla:<36} {self._uno(f'SELECT count(*) FROM {tabla}')}")
            cur.execute("SELECT grupo, count(*) FROM contratos GROUP BY 1 ORDER BY 1")
            w("  por grupo: " + ", ".join(f"{g}={n}" for g, n in cur.fetchall()))
            cur.execute("SELECT servicio, count(*) FROM servicios GROUP BY 1 ORDER BY 1")
            w("  servicios: " + ", ".join(f"{s}={n}" for s, n in cur.fetchall()))

            w("\n== Comprobaciones ==")
            self._comprobar(
                "PPA con un id distinto al de ppa_contratos",
                "SELECT count(*) FROM ppa_contratos p LEFT JOIN contratos c "
                f"ON c.id = p.id AND c.grupo = '{grupos.PPA}' WHERE c.id IS NULL",
            )
            self._comprobar(
                "contratos sin ninguna fila en servicios",
                "SELECT count(*) FROM contratos c WHERE NOT EXISTS "
                "(SELECT 1 FROM servicios s WHERE s.contrato_id = c.id)",
            )

            w("\n== Para revisar en el barrido de servicios ==")
            cur.execute(
                "SELECT id, nombre_interno, servicio_aplica FROM contratos "
                f"WHERE grupo = '{grupos.REPRESENTACION_CGM}' AND tarifa_representacion IS NULL "
                "AND tarifa_cgm IS NOT NULL ORDER BY id"
            )
            filas = cur.fetchall()
            w(f"  Dicen `representacion` pero solo tienen tarifa de CGM: {len(filas)}")
            for f in filas:
                w(f"    contrato {f[0]} ({f[1] or 'sin nombre'}): quedó solo con `cgm`")
            cur.execute(
                "SELECT count(*) FROM contratos "
                f"WHERE grupo = '{grupos.REPRESENTACION_CGM}' AND tarifa_representacion IS NULL "
                "AND tarifa_cgm IS NULL"
            )
            w(f"  Representación sin ninguna tarifa (quedaron con su servicio_aplica): "
              f"{cur.fetchone()[0]}")

    def _comprobar(self, que: str, sql: str) -> None:
        n = self._uno(sql)
        if n:
            raise CommandError(f"{que}: {n}. La copia no es fiel; se deshace.")
        self.stdout.write(f"  OK  {que}: 0")

    # ── utilidades ───────────────────────────────────────────────────────────

    def _uno(self, sql: str):
        self.cur.execute(sql)
        return self.cur.fetchone()[0]

    def _columnas(self, tabla: str) -> set[str]:
        self.cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s", [tabla],
        )
        return {r[0] for r in self.cur.fetchall()}

    def _exigir_catalogo(self) -> None:
        """Todo `servicio_aplica` tiene que estar en el catálogo de `grupos`, o no hay
        grupo que darle. Se dice cuáles en vez de fallar con un NOT NULL."""
        validos = ", ".join(f"'{s}'" for s in grupos.SUBSERVICIOS_DE_CONTRATO_SERVICIO)
        self.cur.execute(
            "SELECT id, servicio_aplica FROM contratos_servicio "
            f"WHERE servicio_aplica IS NULL OR servicio_aplica NOT IN ({validos}) ORDER BY id"
        )
        raros = self.cur.fetchall()
        if raros:
            raise CommandError(
                "Contratos de servicio con un servicio_aplica fuera del catálogo: "
                + ", ".join(f"{i} ({v!r})" for i, v in raros)
            )

    def _exigir_vacias(self, tabla: str) -> None:
        for col in sorted(COLUMNAS_MUERTAS[tabla] & self._columnas(tabla)):
            n = self._uno(f"SELECT count(*) FROM {tabla} WHERE {col} IS NOT NULL")
            if n:
                raise CommandError(
                    f"{tabla}.{col} tiene {n} filas con dato y no tiene destino en "
                    "`contratos`: hay que decidir qué hacer con ella antes de copiar."
                )

    @staticmethod
    def _valor(col: str, alias: str | None = None) -> str:
        ref = f"{alias}.{col}" if alias else col
        if col in DEFAULT_SI_NULO:
            return f"coalesce({ref}, {DEFAULT_SI_NULO[col]})"
        return ref
