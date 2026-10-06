"""Foto de los contratos antes y después del corte (deploy 2), y su comparación.

    docker compose exec operaciones python manage.py verificar_corte_contratos foto
    docker compose exec operaciones python manage.py verificar_corte_contratos comparar \\
        uploads/corte_contratos/foto_antes_<...>.json uploads/corte_contratos/foto_despues_<...>.json

Cuándo (`docs/refactor/08-plan-django-contratos.md`, §6):

1. Deploy 1 y `backfill_contratos_unificados` → `foto` (sale una foto "antes").
2. Deploy 2 (el corte) → `foto` (sale una foto "después").
3. `comparar` las dos. Sin diferencias, termina en 0; con alguna, falla y las lista.

Si el corte salió bien, la base y la app tienen que verse IGUAL, salvo los ids de los
contratos de servicio, que cambiaron (decisión 2) y se traducen con
`contratos_servicio_correspondencia`. Lo que mira cada foto:

- **La base, cruda:** cada llave foránea que apunta a los contratos (nombre, columna,
  definición) y los valores que guarda cada una, con su conteo. Antes apuntan a
  `ppa_contratos` / `contratos_servicio`; después, todas a `contratos`.
- **Lo que ve la app:** cuántos PPA y contratos de servicio hay, los servicios de cada
  contrato, la compra/venta de cada PPA y las plantas de cada PPA.
- **Facturación:** el resumen del cálculo de los períodos pedidos (por defecto, los
  dos meses cerrados anteriores), que recorre PPA, plantas y tarifas.

"Antes" o "después" se decide solo, según si `contratos/0009_corte_bd` está aplicada.
Las fotos van a `uploads/corte_contratos/`: fuera de git (traen datos de contratos y
el repo es público), montada en el contenedor y a salvo del `git reset` del deploy.
Este comando se borra en el deploy 3, con la tabla de correspondencia.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

CORTE = ("contratos", "0009_corte_bd")
VIEJAS = ("ppa_contratos", "contratos_servicio")
NUEVA = "contratos"
SERVICIO = "contratos_servicio"
# Las tablas del propio modelo de contratos: sus llaves no son satélites del corte.
PROPIAS = (
    "ppa_contrato_proyectos", "contrato_proyectos", "servicios",
    "contratos_servicio_correspondencia",
)
CARPETA = os.path.join("uploads", "corte_contratos")


# ── Comparación (pura) ───────────────────────────────────────────────────────


def normalizar_definicion(definicion: str) -> str:
    """La definición de la FK como quedaría apuntando a `contratos`."""
    return re.sub(
        r"REFERENCES (?:public\.)?(?:ppa_contratos|contratos_servicio)\(",
        f"REFERENCES {NUEVA}(", definicion,
    )


def _traducir_valores(valores: dict, apunta_a: str, corr: dict) -> Counter:
    """Los valores de una FK de antes, con los ids de servicio ya en su id nuevo."""
    if apunta_a != SERVICIO:
        return Counter({str(v): n for v, n in valores.items()})
    salida = Counter()
    for v, n in valores.items():
        salida[str(corr.get(str(v), f"sin-correspondencia:{v}"))] += n
    return salida


def comparar(antes: dict, despues: dict) -> list[str]:
    """Las diferencias entre las dos fotos, en frases. Vacío = el corte no cambió nada."""
    diferencias: list[str] = []
    if antes.get("momento") != "antes" or despues.get("momento") != "despues":
        diferencias.append(
            f"Las fotos no son antes/después: {antes.get('momento')!r} y "
            f"{despues.get('momento')!r}."
        )
    corr = despues.get("correspondencia") or antes.get("correspondencia") or {}

    # 1. Llaves foráneas: las mismas, con el mismo nombre y definición.
    fk_antes = {f"{f['tabla']}.{f['columna']}": f for f in antes["fks"]}
    fk_despues = {f"{f['tabla']}.{f['columna']}": f for f in despues["fks"]}
    for clave in sorted(fk_antes.keys() - fk_despues.keys()):
        diferencias.append(f"FK {clave}: estaba antes y ya no está.")
    for clave in sorted(fk_despues.keys() - fk_antes.keys()):
        diferencias.append(f"FK {clave}: apareció después.")
    for clave in sorted(fk_antes.keys() & fk_despues.keys()):
        a, d = fk_antes[clave], fk_despues[clave]
        if d["apunta_a"] != NUEVA:
            diferencias.append(f"FK {clave}: después apunta a {d['apunta_a']}, no a {NUEVA}.")
        if a["nombre"] != d["nombre"]:
            diferencias.append(f"FK {clave}: cambió de nombre ({a['nombre']} → {d['nombre']}).")
        if normalizar_definicion(a["definicion"]) != d["definicion"]:
            diferencias.append(
                f"FK {clave}: cambió la definición ({a['definicion']} → {d['definicion']})."
            )

        # 2. Los valores que guarda: cada fila sigue apuntando al mismo contrato.
        if a["apunta_a"] == SERVICIO and not corr:
            diferencias.append(f"FK {clave}: no hay correspondencia para traducir sus ids.")
            continue
        va = _traducir_valores(antes["valores"].get(clave, {}), a["apunta_a"], corr)
        vd = Counter({str(v): n for v, n in despues["valores"].get(clave, {}).items()})
        if va != vd:
            faltan = dict(va - vd)
            sobran = dict(vd - va)
            diferencias.append(
                f"FK {clave}: cambiaron sus valores (id: filas) — faltan {faltan}, "
                f"sobran {sobran}."
            )

    # 3. Lo que ve la app.
    for campo in ("n_ppa", "n_servicio"):
        if antes.get(campo) != despues.get(campo):
            diferencias.append(f"{campo}: {antes.get(campo)} → {despues.get(campo)}.")

    sub_antes = {
        str(corr.get(k, f"sin-correspondencia:{k}")): v
        for k, v in antes.get("subservicios", {}).items()
    }
    _comparar_mapas("servicios del contrato de servicio", sub_antes,
                    despues.get("subservicios", {}), diferencias)
    _comparar_mapas("compra/venta del PPA", antes.get("subservicio_ppa", {}),
                    despues.get("subservicio_ppa", {}), diferencias)
    _comparar_mapas("plantas del PPA", antes.get("plantas_por_ppa", {}),
                    despues.get("plantas_por_ppa", {}), diferencias)

    # 4. Facturación, en los períodos que estén en las dos.
    fa, fd = antes.get("facturacion", {}), despues.get("facturacion", {})
    comunes = sorted(fa.keys() & fd.keys())
    if not comunes:
        diferencias.append("Facturación: las fotos no tienen ningún período en común.")
    for per in comunes:
        if fa[per] != fd[per]:
            cambios = {
                k: (fa[per].get(k), fd[per].get(k))
                for k in sorted(fa[per].keys() | fd[per].keys())
                if fa[per].get(k) != fd[per].get(k)
            }
            diferencias.append(f"Facturación {per}: cambió (antes, después) {cambios}.")
    return diferencias


def _comparar_mapas(que: str, antes: dict, despues: dict, diferencias: list) -> None:
    for clave in sorted(antes.keys() | despues.keys(), key=str):
        if antes.get(clave) != despues.get(clave):
            diferencias.append(
                f"{que} {clave}: {antes.get(clave)!r} → {despues.get(clave)!r}."
            )


# ── Foto (lee la base) ───────────────────────────────────────────────────────


def _momento(connection) -> str:
    from django.db.migrations.recorder import MigrationRecorder

    aplicadas = MigrationRecorder(connection).applied_migrations()
    return "despues" if CORTE in aplicadas else "antes"


def _periodos_por_defecto() -> list[str]:
    from apps.plataforma.services.fechas import hoy_col

    hoy = hoy_col()
    salida, anio, mes = [], hoy.year, hoy.month
    for _ in range(2):
        anio, mes = (anio - 1, 12) if mes == 1 else (anio, mes - 1)
        salida.append(f"{anio:04d}-{mes:02d}")
    return sorted(salida)


def tomar_foto(periodos: list[str]) -> dict:
    from django.db import connection

    from apps.contratos.models import ContratoServicio
    from apps.contratos.services import grupos
    from apps.facturacion.services import calculo
    from apps.ppa.models import PpaContrato, PpaContratoProyecto

    momento = _momento(connection)
    destino = [NUEVA] if momento == "despues" else list(VIEJAS)
    datos: dict = {"momento": momento}

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT con.conrelid::regclass::text, a.attname,
                   con.confrelid::regclass::text, con.conname,
                   pg_get_constraintdef(con.oid)
              FROM pg_constraint con
              JOIN pg_attribute a
                ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
             WHERE con.contype = 'f'
               AND con.confrelid::regclass::text = ANY(%s)
               AND NOT (con.conrelid::regclass::text = ANY(%s))
             ORDER BY 1, 2
            """,
            [destino, list(PROPIAS)],
        )
        fks = cur.fetchall()
        datos["fks"] = [
            {"tabla": t, "columna": c, "apunta_a": a, "nombre": n, "definicion": d}
            for t, c, a, n, d in fks
        ]
        valores = {}
        for tabla, columna, *_ in fks:
            cur.execute(
                f'SELECT "{columna}", count(*) FROM {tabla} '
                f'WHERE "{columna}" IS NOT NULL GROUP BY 1'
            )
            valores[f"{tabla}.{columna}"] = {str(v): n for v, n in cur.fetchall()}
        datos["valores"] = valores

        cur.execute("SELECT to_regclass('contratos_servicio_correspondencia') IS NOT NULL")
        if cur.fetchone()[0]:
            cur.execute("SELECT id_viejo, contrato_id FROM contratos_servicio_correspondencia")
            datos["correspondencia"] = {str(v): n for v, n in cur.fetchall()}

    datos["n_ppa"] = PpaContrato.objects.count()
    datos["n_servicio"] = ContratoServicio.objects.count()
    datos["subservicios"] = {
        str(c.pk): sorted(grupos.subservicios_de(c))
        for c in ContratoServicio.objects.all()
    }
    datos["subservicio_ppa"] = {
        str(c.pk): grupos.subservicio_de_ppa(c) for c in PpaContrato.objects.all()
    }
    plantas: dict[str, list] = {}
    for v in PpaContratoProyecto.objects.all():
        plantas.setdefault(str(v.contrato_id), []).append(v.proyecto_id)
    datos["plantas_por_ppa"] = {k: sorted(v) for k, v in plantas.items()}

    datos["facturacion"] = {}
    for per in periodos:
        try:
            datos["facturacion"][per] = calculo.periodo(per)["resumen"]
        except Exception as exc:  # que la foto no se caiga: queda anotado y se compara
            datos["facturacion"][per] = {"error": repr(exc)}
    return datos


class Command(BaseCommand):
    help = "Foto de los contratos antes/después del corte, y su comparación."

    def add_arguments(self, parser):
        parser.add_argument("accion", choices=["foto", "comparar"])
        parser.add_argument("rutas", nargs="*",
                            help="comparar: la foto de antes y la de después.")
        parser.add_argument("--periodo", action="append", dest="periodos",
                            help="foto: período de facturación YYYY-MM (repetible). "
                                 "Por defecto, los dos meses cerrados anteriores.")
        parser.add_argument("--carpeta", default=CARPETA,
                            help=f"foto: dónde escribirla (por defecto {CARPETA}).")

    def handle(self, *args, accion, rutas, periodos, carpeta, **opts):
        if accion == "foto":
            self._foto(periodos, carpeta)
        else:
            self._comparar(rutas)

    def _foto(self, periodos, carpeta):
        from apps.facturacion.services.calculo import periodo_valido
        from apps.plataforma.services.fechas import ahora_col

        try:
            periodos = sorted({periodo_valido(p) for p in periodos or []}) \
                or _periodos_por_defecto()
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        datos = tomar_foto(periodos)
        os.makedirs(carpeta, exist_ok=True)
        ruta = os.path.join(
            carpeta, f"foto_{datos['momento']}_{ahora_col():%Y%m%d_%H%M%S}.json"
        )
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=1, default=str)

        errores = [p for p, r in datos["facturacion"].items() if "error" in r]
        self.stdout.write(
            f"Foto «{datos['momento']}»: {len(datos['fks'])} llaves foráneas, "
            f"{datos['n_ppa']} PPA, {datos['n_servicio']} contratos de servicio, "
            f"correspondencia: {len(datos.get('correspondencia', {}))} filas, "
            f"facturación: {', '.join(periodos)}."
        )
        if errores:
            self.stdout.write(self.style.WARNING(
                f"El cálculo de facturación falló en {errores}; quedó anotado en la foto."
            ))
        self.stdout.write(self.style.SUCCESS(f"Escrita en {ruta}"))

    def _comparar(self, rutas):
        if len(rutas) != 2:
            raise CommandError("comparar necesita dos rutas: la foto de antes y la de después.")
        fotos = []
        for ruta in rutas:
            with open(ruta, encoding="utf-8") as f:
                fotos.append(json.load(f))
        diferencias = comparar(*fotos)
        if diferencias:
            for d in diferencias:
                self.stdout.write(f"  - {d}")
            raise CommandError(f"El corte cambió algo: {len(diferencias)} diferencias.")
        antes = fotos[0]
        self.stdout.write(self.style.SUCCESS(
            f"Sin diferencias: {len(antes['fks'])} llaves foráneas con sus valores, "
            f"{antes['n_ppa']} PPA, {antes['n_servicio']} contratos de servicio, "
            f"facturación de {', '.join(sorted(antes['facturacion']))}."
        ))
