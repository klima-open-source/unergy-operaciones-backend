"""Alta, baja y fusión de proyectos, y la confirmación de un pendiente.

Puerto de las escrituras de `app/api/v1/proyectos.py`.

**El borrado de un proyecto es la operación más destructiva de la app.** Por eso
`motivo_bloqueo_borrado` mira DOS cosas: las relaciones que el ORM conoce y las
siete tablas con FK en CASCADE que no tienen relación declarada. Esas siete las
borraba PostgreSQL en cascada sin ningún aviso —historial de generación diaria,
paneles contables ya calculados, registros de conexión CND— hasta la auditoría
de Proyectos del 2026-08-27.

**El merge retrata al perdedor ANTES de tocarlo.** El paso de los escalares
únicos le vacía campos, así que la foto tomada al final saldría mutilada, y es el
único rastro que queda de la planta.
"""

from __future__ import annotations

from django.db import connection, transaction

from api.exceptions import Conflict
from apps.plataforma.services.auditoria import registrar_borrado
from apps.proyectos.models import (
    Proyecto, ProyectoInfoTecnica, ProyectoInversionista, ProyectoInversor,
)

# Relaciones que el ORM sí conoce y que bloquean el borrado.
RELACIONES_BLOQUEANTES = [
    ("fallas", "fallas_por_proyecto_id"),
    ("liquidaciones", "liquidaciones_por_proyecto_id"),
    ("asic_solicitudes", "asic_solicitudes_por_proyecto_id"),
    ("rec_procesos", "rec_procesos_por_proyecto_id"),
    ("contratos_servicio", "contratos_servicio_por_proyecto_id"),
    ("fronteras", "fronteras"),
    ("generacion_diaria", "generacion_diaria_por_proyecto_id"),
]

# Tablas con FK a `proyectos` que NO tienen relación declarada en el modelo. Las
# primeras seis están en ON DELETE CASCADE, así que PostgreSQL las borraba sin
# aviso. `proyecto_informe_om` está en NO ACTION: ahí el riesgo no es perder el
# dato, sino un IntegrityError sin capturar (500 crudo).
#
# `arr_documento` tiene DOS columnas de proyecto: `arr_proyecto_id` (del módulo
# de Arriendos, sin relación con esto) y `proyecto_id`, que es la que importa.
# No incluye `cumplimiento_mensual` (ON DELETE SET NULL: queda huérfana, no se
# pierde el dato).
TABLAS_CASCADE_SIN_RELACION = [
    ("panel_contable", "proyecto_id"),
    ("panel_consecutivo", "proyecto_id"),
    ("clasificacion_energia_mensual", "proyecto_id"),
    ("clasificacion_liquidacion", "proyecto_id"),
    ("registro_conexion", "proyecto_id"),
    ("arr_documento", "proyecto_id"),
    ("proyecto_informe_om", "proyecto_id"),
]

# ── Fusión ───────────────────────────────────────────────────────────────────
# Mueve TODOS los registros hijos del perdedor al ganador sin violar constraints:
#   MERGE_SIMPLE     : 1-a-muchos sin constraint → repunte directo.
#   MERGE_COMPUESTO  : con UNIQUE compuesto → si el ganador ya tiene esa clave, se
#                      descarta la fila del perdedor; el resto se repunta.
#   MERGE_UNO_A_UNO  : `proyecto_id` UNIQUE → si el ganador ya tiene fila, se
#                      descarta la del perdedor.
MERGE_SIMPLE = [
    "proyecto_inversores", "proyecto_inversionistas", "fronteras", "fallas",
    "contratos_servicio", "asic_solicitudes", "rec_procesos",
    "costos_variables", "gestion_registros", "cumplimiento_mensual",
]
MERGE_COMPUESTO = [
    ("generacion_diaria", ["fecha"]),
    ("liquidaciones", ["periodo"]),
    ("panel_contable", ["periodo", "tipo"]),
    ("clasificacion_liquidacion", ["periodo"]),
    ("mapeo_celda_concepto", ["concepto"]),
    ("alias_fuente_ingreso", ["columna_origen"]),
    ("ppa_contrato_proyectos", ["contrato_id"]),
    # UNIQUE (proyecto_id, tipo): si el ganador ya tiene puntero para ese tipo,
    # se descarta el del perdedor.
    ("proyecto_area_contacto", ["tipo"]),
]
MERGE_UNO_A_UNO = ["proyecto_info_tecnica", "proyecto_informe_om"]
MERGE_ESCALAR_UNICO = ["sub_project", "project_id_solenium", "sunfactory_project_id"]
# No-únicos: si el ganador los tiene vacíos, se rellenan con los del perdedor. A
# diferencia de los únicos, no hace falta liberarlos antes: no hay constraint.
MERGE_ESCALAR_SI_VACIO = [
    "municipio", "departamento", "latitud", "longitud", "codigo_tsf",
]


def motivo_bloqueo_borrado(proyecto: Proyecto) -> str | None:
    """`None` si el proyecto se puede borrar; si no, por qué no."""
    for _nombre, relacion in RELACIONES_BLOQUEANTES:
        if getattr(proyecto, relacion).exists():
            return "registros operativos"
    with connection.cursor() as cur:
        for tabla, columna in TABLAS_CASCADE_SIN_RELACION:
            cur.execute(f"SELECT 1 FROM {tabla} WHERE {columna} = %s LIMIT 1", [proyecto.id])
            if cur.fetchone():
                return tabla
    return None


def borrar(proyecto: Proyecto) -> None:
    """Borra el proyecto y sus sub-recursos directos. Borrado FÍSICO: a
    diferencia de Cliente, aquí el modelo no lleva soft-delete para esta ruta."""
    with transaction.atomic():
        for modelo in (ProyectoInversionista, ProyectoInversor, ProyectoInfoTecnica):
            modelo.objects.filter(proyecto_id=proyecto.id).delete()
        from apps.clientes.models import ProyectoAreaContacto

        ProyectoAreaContacto.objects.filter(proyecto_id=proyecto.id).delete()
        proyecto.delete()


def _escalar(cur, sql: str, params: dict) -> int:
    cur.execute(sql, params)
    fila = cur.fetchone()
    return fila[0] if fila else 0


def reporte_merge(ganador: Proyecto, perdedor: Proyecto) -> tuple[list[dict], list[dict]]:
    """Qué filas se moverían y qué escalares se copiarían. No modifica nada."""
    p = {"keeper": ganador.id, "loser": perdedor.id}
    movimientos: list[dict] = []

    with connection.cursor() as cur:
        for t in MERGE_SIMPLE:
            n = _escalar(cur, f"SELECT count(*) FROM {t} WHERE proyecto_id = %(loser)s", p)
            if n:
                movimientos.append({"tabla": t, "a_mover": n, "descartadas_por_colision": 0})

        for t, claves in MERGE_COMPUESTO:
            n = _escalar(cur, f"SELECT count(*) FROM {t} WHERE proyecto_id = %(loser)s", p)
            if not n:
                continue
            cond = " AND ".join(f"k.{c} = {t}.{c}" for c in claves)
            coli = _escalar(
                cur,
                f"SELECT count(*) FROM {t} WHERE proyecto_id = %(loser)s AND EXISTS "
                f"(SELECT 1 FROM {t} k WHERE k.proyecto_id = %(keeper)s AND {cond})",
                p,
            )
            movimientos.append({
                "tabla": t, "a_mover": n - coli, "descartadas_por_colision": coli,
            })

        for t in MERGE_UNO_A_UNO:
            del_perdedor = _escalar(
                cur, f"SELECT count(*) FROM {t} WHERE proyecto_id = %(loser)s", p
            )
            if not del_perdedor:
                continue
            del_ganador = _escalar(
                cur, f"SELECT count(*) FROM {t} WHERE proyecto_id = %(keeper)s", p
            )
            coli = del_perdedor if del_ganador else 0
            movimientos.append({
                "tabla": t, "a_mover": del_perdedor - coli, "descartadas_por_colision": coli,
            })

        # asic_cambios_contratos: doble FK, sin unique.
        asic = sum(
            _escalar(cur, f"SELECT count(*) FROM asic_cambios_contratos WHERE {campo} = %(loser)s", p)
            for campo in ("proyecto_original_id", "proyecto_nuevo_id")
        )
        if asic:
            movimientos.append({
                "tabla": "asic_cambios_contratos", "a_mover": asic,
                "descartadas_por_colision": 0,
            })

    campos_copiados = [
        {"campo": f, "valor": getattr(perdedor, f)}
        for f in MERGE_ESCALAR_UNICO + MERGE_ESCALAR_SI_VACIO
        if getattr(ganador, f, None) in (None, "")
        and getattr(perdedor, f, None) not in (None, "")
    ]
    return movimientos, campos_copiados


def ejecutar_merge(ganador: Proyecto, perdedor: Proyecto, movimientos: list[dict],
                   campos_copiados: list[dict], usuario=None) -> None:
    """Fusión completa en UNA transacción. El perdedor se borra."""
    p = {"keeper": ganador.id, "loser": perdedor.id}

    with transaction.atomic():
        # 0) Retrato del perdedor ANTES de tocarlo: el paso 5 le vacía los campos
        #    únicos, así que la foto tomada al final saldría mutilada.
        registrar_borrado("proyectos", perdedor.id, tipo="hard", usuario=usuario, contexto={
            "operacion": "merge_proyectos",
            "ganador_id": ganador.id,
            "ganador_nombre": ganador.nombre_comercial,
            "movimientos": movimientos,
            "campos_copiados_al_ganador": campos_copiados,
            "total_filas_movidas": sum(m["a_mover"] for m in movimientos),
        })

        with connection.cursor() as cur:
            # 1) Doble FK de ASIC.
            for campo in ("proyecto_original_id", "proyecto_nuevo_id"):
                cur.execute(
                    f"UPDATE asic_cambios_contratos SET {campo} = %(keeper)s "
                    f"WHERE {campo} = %(loser)s", p
                )

            # 2) UNIQUE compuesto: se descarta la colisión, se repunta el resto.
            for t, claves in MERGE_COMPUESTO:
                cond = " AND ".join(f"k.{c} = {t}.{c}" for c in claves)
                cur.execute(
                    f"DELETE FROM {t} WHERE proyecto_id = %(loser)s AND EXISTS "
                    f"(SELECT 1 FROM {t} k WHERE k.proyecto_id = %(keeper)s AND {cond})", p
                )
                cur.execute(
                    f"UPDATE {t} SET proyecto_id = %(keeper)s WHERE proyecto_id = %(loser)s", p
                )

            # 3) 1-a-1: si el ganador ya tiene, se descarta la del perdedor.
            for t in MERGE_UNO_A_UNO:
                cur.execute(
                    f"DELETE FROM {t} WHERE proyecto_id = %(loser)s AND EXISTS "
                    f"(SELECT 1 FROM {t} k WHERE k.proyecto_id = %(keeper)s)", p
                )
                cur.execute(
                    f"UPDATE {t} SET proyecto_id = %(keeper)s WHERE proyecto_id = %(loser)s", p
                )

            # 4) Tablas simples.
            for t in MERGE_SIMPLE:
                cur.execute(
                    f"UPDATE {t} SET proyecto_id = %(keeper)s WHERE proyecto_id = %(loser)s", p
                )

            # 5) Escalares únicos: liberar del perdedor y copiar al ganador.
            for f in MERGE_ESCALAR_UNICO:
                cur.execute(f"UPDATE proyectos SET {f} = NULL WHERE id = %(loser)s", p)
            for c in campos_copiados:
                cur.execute(
                    f"UPDATE proyectos SET {c['campo']} = %(val)s WHERE id = %(keeper)s",
                    {**p, "val": c["valor"]},
                )

            # 6) Borrar el perdedor.
            cur.execute("DELETE FROM proyectos WHERE id = %(loser)s", p)


def sincronizar_fuentes_externas(proyecto: Proyecto) -> None:
    """Los cuatro backfills best-effort que corren al crear o confirmar.

    Ninguno lanza: si una API externa falla, el proyecto queda como estaba. Se
    llaman en el momento de la creación para que los proyectos nuevos no vuelvan
    a acumular los vacíos que el backlog tuvo que rellenar a mano.
    """
    from apps.proyectos.services.backfill_solarview import (
        sincronizar_project_id_solarview_si_aplica,
    )
    from apps.proyectos.services.backfill_solenium import (
        sincronizar_info_tecnica_solenium_si_aplica,
    )
    from apps.proyectos.services.backfill_unergy import sincronizar_datos_unergy_si_aplica
    from apps.proyectos.services.tsf_sync import sincronizar_ubicacion_tsf_si_aplica

    sincronizar_datos_unergy_si_aplica(proyecto)
    sincronizar_ubicacion_tsf_si_aplica(proyecto)
    sincronizar_info_tecnica_solenium_si_aplica(proyecto)
    sincronizar_project_id_solarview_si_aplica(proyecto)
