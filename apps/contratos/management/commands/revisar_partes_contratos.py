"""Lo que hay que resolver ANTES de borrar el nombre y el NIT escritos en los contratos.

**Por defecto solo lee.** Escribe únicamente con `--aplicar`.

    python manage.py revisar_partes_contratos
    python manage.py revisar_partes_contratos --aplicar

Para qué. Las partes de un contrato pasan a ser solo «este cliente, en este papel»
(`contrato_partes`); el nombre y el NIT se leen de la ficha del cliente. Las columnas
`comprador_*`, `vendedor_*`, `contratante_*` y `prestador_*` de `contratos` se borran
(decisión de Sara, 2026-10-07). Lo que hoy SOLO está escrito en esas columnas se
perdería, así que primero se pasa a las fichas:

1. **El NIT que la ficha no tiene.** El contrato lo trae escrito y la ficha del cliente
   vinculado está vacía (Unergy, entre otros): se le pone a la ficha.
2. **Las partes que son solo un nombre**, sin cliente. Se vinculan al cliente que ya
   existe —por NIT, por el mismo nombre, o a Unergy si el nombre es de Unergy— y, si no
   existe ninguno, se le crea la ficha con el nombre y el NIT del contrato. Un nombre
   que solo se PARECE a un cliente no se resuelve solo: puede ser otra empresa.

Y lista, sin tocarlo, lo que necesita una persona:

- **NIT en conflicto**: el escrito en el contrato y el de la ficha son distintos (no
  solo por el dígito de verificación). ¿Está mal el NIT o está mal el vínculo?
- Nombres que solo se parecen a un cliente.
- Partes vinculadas a una ficha borrada.

Las columnas se pueden borrar cuando esto dé **cero pendientes**. Se puede repetir.
"""
from collections import defaultdict

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.clientes.models import Cliente
from apps.clientes.services.gestion import normalizar_nit
from apps.comun.nombre_matching import core_tokens
from apps.contratos.models import Contrato
from apps.contratos.services import partes as partes_service
from apps.proyectos.models import Proyecto

ROLES = ("comprador", "vendedor", "contratante", "prestador")


def mismo_salvo_dv(a: str, b: str) -> bool:
    """El mismo NIT con y sin dígito de verificación ("901822561" / "9018225616")."""
    corto, largo = sorted((a, b), key=len)
    return len(largo) == len(corto) + 1 and largo.startswith(corto)


def otro_dv(a: str, b: str) -> bool:
    """El mismo número con OTRO dígito de verificación ("8300545390" / "8300545391"):
    uno de los dos está mal escrito, o los dos son de la misma entidad."""
    return a != b and len(a) == len(b) > 6 and a[:-1] == b[:-1]


#: Palabras que llevan los nombres de planta y no dicen cuál es.
_DE_PLANTA = frozenset({"minigranja", "granja", "solar", "mgs", "gd", "psf", "planta"})


def _clave_planta(nombre: str) -> frozenset:
    return frozenset(
        p for p in partes_service._palabras(nombre) if p not in _DE_PLANTA and not p.isdigit()
    )


def _clave_nombre(nombre: str) -> frozenset:
    return partes_service._palabras(nombre)


class Command(BaseCommand):
    help = "Revisa nombre y NIT de las partes antes de borrar esas columnas (solo lee sin --aplicar)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--aplicar", action="store_true",
            help="Pone los NIT que faltan en las fichas, vincula y crea fichas. Sin esto, solo informa.",
        )

    def handle(self, *args, aplicar=False, **opts):
        self.out = self.stdout.write
        clientes = {c.id: c for c in Cliente.objects.all()}
        unergy_nit = normalizar_nit(getattr(settings, "UNERGY_NIT", None))
        campos = ["id"] + [f"{r}_{s}" for r in ROLES for s in ("id", "nombre", "nit")]
        contratos = list(Contrato.objects.values(*campos).order_by("id"))

        # ── 1 · NIT escrito en el contrato contra el de la ficha ─────────────
        propuestos = defaultdict(set)   # cliente_id -> NIT que traen sus contratos
        conflictos = []                  # (contrato, rol, cliente, escrito, ficha)
        borrados = []
        for c in contratos:
            for rol in ROLES:
                cid = c[f"{rol}_id"]
                if not cid:
                    continue
                ficha = clientes.get(cid)
                if ficha is None or ficha.deleted_at:
                    borrados.append((c["id"], rol, cid))
                    continue
                escrito = normalizar_nit(c[f"{rol}_nit"])
                if not escrito:
                    continue
                de_ficha = normalizar_nit(ficha.nit_cedula)
                if not de_ficha:
                    propuestos[cid].add(escrito)
                elif escrito != de_ficha and not mismo_salvo_dv(escrito, de_ficha):
                    conflictos.append((c["id"], rol, cid, escrito, de_ficha))

        nit_de = {}  # NIT normalizado -> cliente_id, con las fichas de hoy
        for ficha in clientes.values():
            if ficha.deleted_at is None and (n := normalizar_nit(ficha.nit_cedula)):
                nit_de[n] = ficha.id

        poner_nit = {}  # cliente_id -> NIT
        pedido_por = defaultdict(set)
        for cid, nits in propuestos.items():
            for n in nits:
                pedido_por[n].add(cid)
        for cid, nits in propuestos.items():
            # Con y sin dígito de verificación es uno solo: se queda el completo.
            nits = {n for n in nits if not any(o != n and mismo_salvo_dv(o, n) and len(o) > len(n)
                                               for o in nits)}
            if len(nits) > 1:
                conflictos.append((None, "varios", cid, " / ".join(sorted(nits)), None))
                continue
            nit = nits.pop()
            if nit in nit_de and nit_de[nit] != cid:
                conflictos.append((None, "ya_usado", cid, nit, nit_de[nit]))
                continue
            otros = pedido_por[nit] - {cid}
            if otros:
                conflictos.append((None, "compartido", cid, nit, min(otros)))
                continue
            casi = next((o for n, o in nit_de.items() if otro_dv(nit, n)), None)
            if casi:
                conflictos.append((None, "otro_dv", cid, nit, casi))
                continue
            poner_nit[cid] = nit
        for cid, nit in poner_nit.items():
            nit_de[nit] = cid

        # ── 2 · Partes que son solo un nombre ────────────────────────────────
        unergy_id = nit_de.get(unergy_nit) if unergy_nit else None
        vincular = []          # (contrato, rol, cliente_id, cómo)
        crear = {}             # clave -> {"nombre", "nit", "partes": [(contrato, rol)]}
        parecidos = []
        # Un nombre de planta ("Minigranja Solar Cañahuate" = "MGS 0005 Cañahuate") no
        # es una empresa: crearle una ficha de cliente estaría mal.
        plantas = {}
        for p in Proyecto.objects.exclude(nombre_comercial__isnull=True).only("id", "nombre_comercial"):
            if clave := _clave_planta(p.nombre_comercial):
                plantas.setdefault(clave, p.nombre_comercial)
        for c in contratos:
            for rol in ROLES:
                nombre = (c[f"{rol}_nombre"] or "").strip()
                if c[f"{rol}_id"] or not nombre:
                    continue
                nit = normalizar_nit(c[f"{rol}_nit"])
                if nit and nit in nit_de:
                    vincular.append((c["id"], rol, nit_de[nit], "NIT"))
                    continue
                if unergy_id and "unergy" in core_tokens(nombre):
                    vincular.append((c["id"], rol, unergy_id, "es Unergy"))
                    continue
                cid, como = partes_service.emparejar_cliente(nombre, nit)
                if como in (partes_service.POR_NIT, partes_service.POR_NOMBRE):
                    vincular.append((c["id"], rol, cid, "mismo nombre"))
                    continue
                if como == partes_service.PARECIDO:
                    parecidos.append((c["id"], rol, nombre, clientes[cid].razon_social_nombre))
                    continue
                if (planta := plantas.get(_clave_planta(nombre))) and (
                    "minigranja" in core_tokens(nombre) or _clave_nombre(nombre) == _clave_nombre(planta)
                ):
                    parecidos.append((c["id"], rol, nombre, f"la PLANTA {planta}"))
                    continue
                clave = ("nit", nit) if nit else ("nombre", _clave_nombre(nombre))
                nueva = crear.setdefault(clave, {"nombre": nombre, "nit": nit, "partes": []})
                nueva["partes"].append((c["id"], rol))

        # ── Informe ──────────────────────────────────────────────────────────
        nombre_de = lambda cid: clientes[cid].razon_social_nombre if cid in clientes else f"#{cid}"
        out = self.out
        out("Revisión de partes antes de borrar nombre y NIT de los contratos")
        out(f"  Fichas a las que se les pone el NIT del contrato : {len(poner_nit)}")
        out(f"  Partes solo-nombre que se vinculan a una ficha   : {len(vincular)}")
        out(f"  Fichas nuevas a crear                            : {len(crear)}"
            f"  ({sum(len(n['partes']) for n in crear.values())} partes)")
        pendientes = len(conflictos) + len(parecidos) + len(borrados)
        out(f"  PENDIENTES (necesitan una persona)               : {pendientes}")

        if poner_nit:
            out("\nNIT que se le pone a la ficha:")
            for cid, nit in sorted(poner_nit.items()):
                out(f"  cliente {cid} {nombre_de(cid)}  <-  {nit}")
        if vincular:
            out("\nSe vinculan a una ficha que ya existe:")
            for cont, rol, cid, como in vincular:
                out(f"  contrato {cont} {rol}: -> {nombre_de(cid)} (cliente {cid}, {como})")
        if crear:
            out("\nFichas nuevas:")
            for n in crear.values():
                donde = ", ".join(f"{cont} {rol}" for cont, rol in n["partes"])
                out(f"  {n['nombre']!r} NIT {n['nit'] or '-'}  (contratos: {donde})")
        if conflictos:
            out("\nPENDIENTE · NIT en conflicto (no se toca):")
            for cont, rol, cid, escrito, otro in conflictos:
                if rol == "varios":
                    out(f"  cliente {cid} {nombre_de(cid)}: sus contratos traen NIT distintos: {escrito}")
                elif rol == "compartido":
                    out(f"  cliente {cid} {nombre_de(cid)}: sus contratos traen {escrito}, el mismo "
                        f"NIT que los de {nombre_de(otro)} (cliente {otro})")
                elif rol == "otro_dv":
                    out(f"  cliente {cid} {nombre_de(cid)}: sus contratos traen {escrito}; "
                        f"{nombre_de(otro)} (cliente {otro}) tiene el mismo número con otro "
                        "dígito de verificación")
                elif rol == "ya_usado":
                    out(f"  cliente {cid} {nombre_de(cid)}: sus contratos traen {escrito}, "
                        f"que es el NIT de {nombre_de(otro)} (cliente {otro})")
                else:
                    pista = ""
                    if escrito == unergy_nit:
                        pista = "  <- el escrito es el NIT de Unergy"
                    elif escrito in nit_de:
                        pista = f"  <- el escrito es el NIT de {nombre_de(nit_de[escrito])}"
                    out(f"  contrato {cont} {rol}: escrito {escrito}, ficha {nombre_de(cid)} "
                        f"tiene {otro}{pista}")
        if parecidos:
            out("\nPENDIENTE · no se crea ficha: se parece a un cliente o es el nombre de una planta:")
            for cont, rol, nombre, sugerido in parecidos:
                out(f"  contrato {cont} {rol}: {nombre!r} -> ¿{sugerido}?")
        if borrados:
            out("\nPENDIENTE · vinculado a una ficha borrada:")
            for cont, rol, cid in borrados:
                out(f"  contrato {cont} {rol}: cliente {cid}")

        if not aplicar:
            out("\nSimulación: no se escribió nada. Repite con --aplicar.")
            return

        with transaction.atomic():
            for cid, nit in poner_nit.items():
                Cliente.objects.filter(pk=cid).update(nit_cedula=nit)
            for n in crear.values():
                ficha = Cliente.objects.create(razon_social_nombre=n["nombre"], nit_cedula=n["nit"])
                vincular.extend((cont, rol, ficha.id, "nueva") for cont, rol in n["partes"])
            for cont, rol, cid, _ in vincular:
                contrato = Contrato.objects.get(pk=cont)
                setattr(contrato, f"{rol}_id", cid)
                contrato.save(update_fields=[f"{rol}_id"])  # y Contrato.save() registra la parte
        out(self.style.SUCCESS(
            f"\nAplicado: {len(poner_nit)} NIT, {len(crear)} fichas nuevas, "
            f"{len(vincular)} partes vinculadas. Quedan {pendientes} pendientes."
        ))
