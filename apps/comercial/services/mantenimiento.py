"""Las tres operaciones de mantenimiento del CRM: backfill, dedup y actualización.

Puerto de `_ejecutar_backfill`, `dedup_clientes` y `aplicar_actualizacion` de
`app/api/v1/comercial.py`. Las tres son de admin y las tres son `dry_run=True`
por defecto: escriben sobre el pipeline entero y un error ahí no se ve hasta que
alguien nota que faltan negocios.

**Ninguna borra físico.** El dedup hace soft-delete del prospecto y de sus
oportunidades (`deleted_at`), así que revertir es poner esos campos en NULL.
"""

from __future__ import annotations

from django.db import transaction

from apps.clientes.models import Cliente
from apps.clientes.services.panel import proyectos_por_cliente
from apps.comercial.models import (
    Oportunidad, OportunidadEstadoHistorial, OportunidadOferta,
    OportunidadOfertaProyecto,
)
from apps.comercial.services.pipeline import col_now, estado_a_resultado
from apps.comun.nombre_matching import mejor_candidato
from apps.contratos.models import ContratoParte
from apps.proyectos.models import Proyecto, ProyectoInversionista


def _ids(qs, campo: str) -> set[int]:
    return {
        v for v in qs.filter(**{f"{campo}__isnull": False})
        .values_list(campo, flat=True).distinct()
        if v is not None
    }


def ejecutar_backfill(usuario_id: int, dry_run: bool = True,
                      solo_con_relacion_comercial: bool = False) -> dict:
    """Una oportunidad en 'operando' por cliente existente sin oportunidad,
    vinculando sus proyectos vía `ProyectoInversionista`. Idempotente.

    El vínculo a los proyectos se hace creando una oferta "operando" y
    conectándola por la M2M — el mismo mecanismo que usa el resto del pipeline.
    Antes se escribía directo `Proyecto.oportunidad_id`, una columna que nunca
    tuvo otro lector real y que se eliminó (0/188 poblada).
    """
    con_oportunidad = _ids(
        Oportunidad.objects.filter(deleted_at__isnull=True), "cliente_id"
    )
    a_migrar = list(
        Cliente.objects
        .filter(deleted_at__isnull=True)
        .exclude(id__in=con_oportunidad)
        .order_by("id")
    )

    if solo_con_relacion_comercial:
        # Oportunidad es el pipeline COMERCIAL: un inversionista puro —sin
        # contrato de servicio ni PPA— nunca pasó por una negociación, así que
        # crearle una oportunidad "operando" sería un registro sin sustento y
        # ensuciaría los KPIs del CRM. El job diario solo migra a quien de
        # verdad tiene relación comercial.
        con_contrato = _ids(ContratoParte.objects.all(), "cliente_id")
        a_migrar = [c for c in a_migrar if c.id in con_contrato]

    resumen = {"clientes_a_migrar": len(a_migrar), "proyectos_a_vincular": 0, "detalle": []}
    ahora = col_now()

    with transaction.atomic():
        for c in a_migrar:
            proyecto_ids = list(
                ProyectoInversionista.objects
                .filter(cliente_id=c.id, proyecto__deleted_at__isnull=True)
                .values_list("proyecto_id", flat=True)
                .distinct()
            )
            resumen["proyectos_a_vincular"] += len(proyecto_ids)
            resumen["detalle"].append({
                "cliente_id": c.id, "razon_social": c.razon_social_nombre,
                "proyectos": len(proyecto_ids),
            })
            if dry_run:
                continue

            op = Oportunidad.objects.create(
                cliente_id=c.id, estado="operando", estado_desde=ahora,
                es_migrada=True, creado_por_usuario_id=usuario_id,
            )
            OportunidadEstadoHistorial.objects.create(
                oportunidad_id=op.id, estado_anterior=None,
                estado_nuevo="operando", usuario_id=usuario_id,
            )
            if proyecto_ids:
                oferta = OportunidadOferta.objects.create(
                    oportunidad_id=op.id, tipo="servicios_operacionales",
                    estado="operando", estado_desde=ahora,
                    resultado=estado_a_resultado("operando"),
                    fecha_oferta=ahora.date(),
                )
                OportunidadOfertaProyecto.objects.bulk_create([
                    OportunidadOfertaProyecto(oferta_id=oferta.id, proyecto_id=pid)
                    for pid in proyecto_ids
                ])

    resumen["dry_run"] = dry_run
    return resumen


def dedup_clientes(usuario_id: int, dry_run: bool = True, umbral: float = 0.85) -> dict:
    """Limpia los clientes-prospecto que el import creó por duplicado cuando ya
    existía el cliente operativo. CONSERVADOR y REVERSIBLE.

    - **candidato**: cliente con `origen_tipo` NULL + oportunidad `es_migrada` con
      ≥1 oferta + SIN huella operativa (no inversionista, ni contrato, ni PPA);
      o sea, un prospecto puro creado por el import.
    - **canónico**: por el matcher difuso compartido, que tiene guarda de
      ambigüedad — (a) la planta de alguna de sus ofertas coincide con un
      Proyecto cuyo dueño es un cliente NO prospecto → ese dueño; o (b) la razón
      social coincide con otro cliente no prospecto. Sin match confiable y único,
      se deja intacto.
    - **acción**: mueve las ofertas al canónico y hace soft-delete del prospecto
      y su oportunidad.
    """
    # Candidatos: clientes con una oportunidad `es_migrada` que tiene ofertas.
    candidatos = set(
        Oportunidad.objects
        .filter(deleted_at__isnull=True, es_migrada=True)
        .filter(id__in=OportunidadOferta.objects.values("oportunidad_id"))
        .values_list("cliente_id", flat=True)
        .distinct()
    )
    huella = proyectos_por_cliente(candidatos) if candidatos else {}
    prospectos = {
        c.id for c in Cliente.objects.filter(
            id__in=candidatos, origen_tipo__isnull=True, deleted_at__isnull=True,
        )
        if not huella.get(c.id)
    } if candidatos else set()

    # Candidatos para el matcher difuso: tolera tildes y typos, ignora el ruido
    # del sector (solar/granja/gd…) y NO adivina si dos quedan parejos.
    proyectos_item = [
        (pid, [nombre]) for pid, nombre in Proyecto.objects
        .filter(deleted_at__isnull=True)
        .values_list("id", "nombre_comercial")
        if nombre
    ]
    duenos: dict[int, list[int]] = {}
    for pid, cid in ProyectoInversionista.objects.values_list("proyecto_id", "cliente_id"):
        duenos.setdefault(pid, []).append(cid)
    clientes_item = [
        (c.id, [c.razon_social_nombre])
        for c in Cliente.objects.filter(deleted_at__isnull=True)
        if c.id not in prospectos and c.razon_social_nombre
    ]

    salida = {
        "dry_run": dry_run, "prospectos": len(prospectos), "fusionados": 0,
        "sin_canonico": 0, "detalle": [], "sin_canonico_nombres": [],
    }
    if not prospectos:
        return salida

    with transaction.atomic():
        for prospecto in Cliente.objects.filter(id__in=prospectos):
            ofertas = list(
                OportunidadOferta.objects.filter(
                    oportunidad__cliente_id=prospecto.id,
                    oportunidad__deleted_at__isnull=True,
                )
            )
            canonico = proyecto_match = regla = None

            # 1) La planta de alguna oferta —o la razón social, útil cuando la
            #    "empresa" de la hoja era el nombre de la planta— matchea un
            #    Proyecto: su dueño operativo es el canónico.
            nombres = [o.planta_nombre for o in ofertas if o.planta_nombre]
            nombres.append(prospecto.razon_social_nombre)
            for nombre in nombres:
                pid, score = mejor_candidato(nombre, proyectos_item)
                if pid and score >= umbral:
                    for dueno in duenos.get(pid, []):
                        if dueno not in prospectos and dueno != prospecto.id:
                            canonico, proyecto_match = dueno, pid
                            regla = f"planta→dueño ({score})"
                            break
                if canonico:
                    break

            # 2) Si no, la razón social matchea directo a un cliente operativo.
            if not canonico:
                cid, score = mejor_candidato(prospecto.razon_social_nombre, clientes_item)
                if cid and cid != prospecto.id and score >= umbral:
                    canonico, regla = cid, f"nombre ({score})"

            if not canonico:
                salida["sin_canonico"] += 1
                if len(salida["sin_canonico_nombres"]) < 80:
                    salida["sin_canonico_nombres"].append(prospecto.razon_social_nombre)
                continue

            salida["fusionados"] += 1
            salida["detalle"].append({
                "prospecto_id": prospecto.id, "prospecto": prospecto.razon_social_nombre,
                "canonico_id": canonico, "regla": regla,
                "ofertas": len(ofertas), "proyecto": proyecto_match,
            })
            if dry_run:
                continue

            # La oportunidad destino del canónico: se reusa o se crea.
            destino = (
                Oportunidad.objects
                .filter(cliente_id=canonico, deleted_at__isnull=True)
                .order_by("id").first()
            )
            if not destino:
                destino = Oportunidad.objects.create(
                    cliente_id=canonico, estado="operando", estado_desde=col_now(),
                    es_migrada=True, creado_por_usuario_id=usuario_id,
                )
                OportunidadEstadoHistorial.objects.create(
                    oportunidad_id=destino.id, estado_anterior=None,
                    estado_nuevo="operando", usuario_id=usuario_id,
                )
            for o in ofertas:
                o.oportunidad_id = destino.id
                if proyecto_match and o.proyecto_id is None:
                    o.proyecto_id = proyecto_match
            if ofertas:
                OportunidadOferta.objects.bulk_update(
                    ofertas, ["oportunidad_id", "proyecto_id"]
                )

            # Soft-delete del prospecto y de sus oportunidades, ya vacías.
            ahora = col_now()
            Oportunidad.objects.filter(
                cliente_id=prospecto.id, deleted_at__isnull=True,
            ).update(deleted_at=ahora)
            prospecto.deleted_at = ahora
            prospecto.save(update_fields=["deleted_at"])

    return salida
