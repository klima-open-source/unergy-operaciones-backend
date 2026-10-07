"""Alta, baja y fusión de clientes.

Puerto de la parte de `app/api/v1/clientes.py` que no es CRUD directo: el aviso
de duplicado al crear, el motivo por el que un cliente no se puede borrar, y la
fusión de dos clientes.

**El borrado de un cliente es SIEMPRE lógico.** Era el único borrado físico de
`Cliente` en toda la API (auditoría de Clientes, 2026-08-28), inconsistente con
`merge` y con los ~14 sitios que ya filtran por `deleted_at IS NULL`. Con
soft-delete, además, contactos/servicios/documentos ya no se pierden: poner
`deleted_at` en NULL deja al cliente exactamente como estaba.
"""

from __future__ import annotations

import re

from django.db import connection, transaction
from django.utils import timezone

from apps.clientes.models import Cliente, ProyectoAreaContacto
from apps.comun.nombre_matching import mejor_candidato
from apps.plataforma.services.auditoria import registrar_borrado

# Tablas con FK a `clientes.id` en NO ACTION que de verdad deben BLOQUEAR el
# borrado: representan una relación de negocio real, no un log. Antes el único
# mensaje posible asumía siempre "es inversionista de un proyecto", aunque en
# realidad lo bloqueara una Oportunidad. `email_envios` (otro NO ACTION, un log
# de correos sin relación de negocio) se corrigió aparte a SET NULL en la
# revisión 120 — no debía bloquear nunca.
TABLAS_BLOQUEAN_BORRADO = [
    ("proyecto_inversionistas", "cliente_id", "es inversionista de uno o más proyectos"),
    ("oportunidades", "cliente_id", "tiene una o más oportunidades comerciales registradas"),
]

# Fusión: mismo patrón que /proyectos/{ganador}/merge/{perdedor}. `dry_run` por
# defecto, mueve las filas relacionadas, resuelve colisiones quedándose con la
# del ganador, y NUNCA borra físico.
#
# Toda tabla con FK a `clientes` tiene que estar aquí o en `MERGE_COMPUESTO`: la que
# falte se queda apuntando a la ficha borrada, sin error. Las tres últimas faltaban
# hasta el 2026-10-07 (`tests/test_clientes_fusion_completa.py` lo vigila contra el
# modelo). `email_envios` queda fuera a propósito: es un log de correos.
MERGE_SIMPLE = [  # (tabla, columna)
    ("cliente_documentos_comerciales", "cliente_id"),
    ("oportunidades", "cliente_id"),
    ("proyecto_area_contacto", "cliente_id"),
    ("arr_arrendador", "cliente_id"),            # el dueño del terreno; de él sale su IVA
    ("contrato_factura", "inversionista_id"),    # a quién se le emitió la factura
]
MERGE_COMPUESTO = [
    ("contactos", ["email", "tipo"]),               # UNIQUE (cliente_id, email, tipo)
    ("proyecto_inversionistas", ["proyecto_id"]),   # no duplicar al cliente en el mismo proyecto
    # Las partes de los contratos. La fusión mueve las columnas de `contratos` con SQL
    # directo, que no pasa por `Contrato.save()`, así que esta tabla se mueve aquí: si
    # el ganador ya era esa parte de ese contrato, la fila del perdedor sobra.
    ("contrato_partes", ["contrato_id", "rol"]),
    # IVA y retenciones por servicio (y por planta, o general si `proyecto_id` es
    # nulo): si las dos fichas tienen tasa para lo mismo, gana la del ganador.
    ("cliente_tasa_servicio", ["proyecto_id", "servicio"]),
]
# `nit_cedula` es UNIQUE en la base: hay que liberarlo en el perdedor antes de
# copiarlo al ganador (mismo tratamiento que `sunfactory_project_id` en proyectos).
MERGE_ESCALAR_UNICO = ["nit_cedula"]
MERGE_ESCALAR_SI_VACIO = [
    "direccion", "ciudad", "departamento", "tipo_persona", "representante_legal",
]


def normalizar_nit(valor: str | None) -> str | None:
    """El NIT con solo sus dígitos, o None si no queda nada.

    El UNIQUE de `clientes.nit_cedula` compara TEXTO: sin esto,
    "900.123.456-7", "900123456-7" y "9001234567" son tres clientes distintos
    para la base. Vive acá y no en el serializer porque hay tres caminos que
    crean clientes --la API, el CRM (`apps/comercial/services/escritura.py`) y
    la carga de prospectos-- y el que no normalice abre el hueco para todos:
    una fila cruda no choca con las normalizadas.

    No intenta quitar ni agregar el dígito de verificación: "900123456" y
    "9001234567" siguen siendo distintos, porque el último dígito de una cédula
    de 10 cifras es parte del número y adivinar acá significa rechazar a un
    cliente legítimo.
    """
    digitos = re.sub(r"\D", "", valor or "")
    return digitos or None


def buscar_duplicado(razon_social_nombre: str | None, excluir_id: int | None = None):
    """Un cliente ya existente con nombre muy parecido.

    Mismo algoritmo de tokens+similitud que proyectos y fronteras. Es
    deliberadamente PERMISIVO —puede marcar como parecidas dos empresas que
    comparten una palabra común—: el aviso no bloquea, solo exige confirmar
    "crear de todos modos". El caso que lo motivó: la migración del CRM creó
    "Quantum" cuando ya existía "Quantum Energy Ingenieria S.A.S.", que un match
    exacto de nombre no habría detectado.
    """
    if not razon_social_nombre:
        return None
    consulta = Cliente.objects.filter(deleted_at__isnull=True)
    if excluir_id:
        consulta = consulta.exclude(pk=excluir_id)
    candidatos = [(c, [c.razon_social_nombre]) for c in consulta]
    match, _score = mejor_candidato(razon_social_nombre, candidatos)
    return match


def motivo_bloqueo_borrado(cliente_id: int) -> str | None:
    with connection.cursor() as cur:
        for tabla, columna, motivo in TABLAS_BLOQUEAN_BORRADO:
            cur.execute(f"SELECT 1 FROM {tabla} WHERE {columna} = %s LIMIT 1", [cliente_id])
            if cur.fetchone():
                return motivo
    return None


def borrar(cliente: Cliente) -> None:
    """Soft-delete. Limpia antes los punteros de área que apunten al cliente:
    son vínculos inofensivos y el proyecto vuelve a usar sus inversionistas."""
    ProyectoAreaContacto.objects.filter(cliente_id=cliente.id).delete()
    cliente.deleted_at = timezone.now()
    cliente.save(update_fields=["deleted_at"])


#: Las columnas de `contratos` que apuntan a un cliente como parte del contrato.
CAMPOS_PARTE_CONTRATO = (
    "comprador_id", "vendedor_id",                          # PPA
    "contratante_id", "prestador_id", "inversionista_id",   # servicio
)


def _escalar(cur, sql: str, params: dict) -> int:
    cur.execute(sql, params)
    fila = cur.fetchone()
    return fila[0] if fila else 0


def reporte_merge(ganador: Cliente, perdedor: Cliente) -> tuple[list[dict], list[dict]]:
    """Qué filas se moverían y qué campos escalares se copiarían. No modifica nada."""
    p = {"keeper": ganador.id, "loser": perdedor.id}
    movimientos: list[dict] = []

    with connection.cursor() as cur:
        for t, col in MERGE_SIMPLE:
            n = _escalar(cur, f"SELECT count(*) FROM {t} WHERE {col} = %(loser)s", p)
            if n:
                movimientos.append({"tabla": t, "a_mover": n, "descartadas_por_colision": 0})

        for t, claves in MERGE_COMPUESTO:
            n = _escalar(cur, f"SELECT count(*) FROM {t} WHERE cliente_id = %(loser)s", p)
            if not n:
                continue
            cond = " AND ".join(f"k.{c} IS NOT DISTINCT FROM {t}.{c}" for c in claves)
            coli = _escalar(
                cur,
                f"SELECT count(*) FROM {t} WHERE cliente_id = %(loser)s AND EXISTS "
                f"(SELECT 1 FROM {t} k WHERE k.cliente_id = %(keeper)s AND {cond})",
                p,
            )
            movimientos.append({
                "tabla": t, "a_mover": n - coli, "descartadas_por_colision": coli,
            })

        # contratos: las partes de PPA (comprador/vendedor) y de servicio
        # (contratante/prestador/inversionista), todas en la tabla única desde el
        # corte (plan 08). Sin unicidad por cliente. Las de servicio faltaban acá
        # hasta la auditoría de Clientes 2026-08-27: fusionar un cliente que fuera
        # parte de un contrato de servicio lo dejaba apuntando al perdedor.
        n = sum(
            _escalar(cur, f"SELECT count(*) FROM contratos WHERE {campo} = %(loser)s", p)
            for campo in CAMPOS_PARTE_CONTRATO
        )
        if n:
            movimientos.append({
                "tabla": "contratos", "a_mover": n, "descartadas_por_colision": 0,
            })

    campos_copiados = [
        {"campo": f, "valor": getattr(perdedor, f)}
        for f in MERGE_ESCALAR_UNICO + MERGE_ESCALAR_SI_VACIO
        if getattr(ganador, f, None) in (None, "")
        and getattr(perdedor, f, None) not in (None, "")
    ]
    return movimientos, campos_copiados


def ejecutar_merge(ganador: Cliente, perdedor: Cliente, movimientos: list[dict],
                   campos_copiados: list[dict], usuario=None) -> None:
    """Fusión completa en UNA transacción. El perdedor queda con soft-delete."""
    p = {"keeper": ganador.id, "loser": perdedor.id}

    with transaction.atomic():
        # 0) Retrato del perdedor ANTES de tocarlo: el paso 4 le vacía los campos
        #    únicos y más abajo la foto saldría mutilada.
        registrar_borrado("clientes", perdedor.id, tipo="soft", usuario=usuario, contexto={
            "operacion": "merge_clientes",
            "ganador_id": ganador.id,
            "ganador_nombre": ganador.razon_social_nombre,
            "movimientos": movimientos,
            "campos_copiados_al_ganador": campos_copiados,
            "total_filas_movidas": sum(m["a_mover"] for m in movimientos),
        })

        with connection.cursor() as cur:
            # 1) Las cinco columnas de parte de `contratos`.
            for campo in CAMPOS_PARTE_CONTRATO:
                cur.execute(
                    f"UPDATE contratos SET {campo} = %(keeper)s WHERE {campo} = %(loser)s", p
                )

            # 2) Colisión por clave compuesta: se descarta la del perdedor.
            for t, claves in MERGE_COMPUESTO:
                cond = " AND ".join(f"k.{c} IS NOT DISTINCT FROM {t}.{c}" for c in claves)
                cur.execute(
                    f"DELETE FROM {t} WHERE cliente_id = %(loser)s AND EXISTS "
                    f"(SELECT 1 FROM {t} k WHERE k.cliente_id = %(keeper)s AND {cond})", p
                )
                cur.execute(f"UPDATE {t} SET cliente_id = %(keeper)s WHERE cliente_id = %(loser)s", p)

            # 3) Tablas simples.
            for t, col in MERGE_SIMPLE:
                cur.execute(f"UPDATE {t} SET {col} = %(keeper)s WHERE {col} = %(loser)s", p)

            # 4) Escalares únicos: liberar del perdedor y copiar al ganador.
            for f in MERGE_ESCALAR_UNICO:
                cur.execute(f"UPDATE clientes SET {f} = NULL WHERE id = %(loser)s", p)
            for c in campos_copiados:
                cur.execute(
                    f"UPDATE clientes SET {c['campo']} = %(val)s WHERE id = %(keeper)s",
                    {**p, "val": c["valor"]},
                )

            # 5) Baja del perdedor: soft-delete, nunca físico.
            cur.execute("UPDATE clientes SET deleted_at = NOW() WHERE id = %(loser)s", p)
