"""A qué correos se le notifica de un proyecto o de un cliente.

Solo la parte que necesita el módulo de informes; el resto de
`app/services/contactos.py` se portará con los recursos que lo usen.
"""

from datetime import date

from django.db.models import Q

from apps.clientes import models as cl_models
from apps.proyectos import models as py_models


def correos(tipo: str, *, proyecto_id=None, cliente_id=None) -> list[str]:
    """Correos de ese tipo. Exactamente uno de `proyecto_id` o `cliente_id`.

    Para un proyecto hay dos caminos y el orden importa:

    1. Si el proyecto tiene un **puntero de área** para ese tipo, se usa SOLO el
       cliente al que apunta. Es la forma de decir «para operación, escríbanle a
       este y a nadie más».
    2. Si no lo tiene, se usa la unión de los contactos de todos sus
       inversionistas VIGENTES, sin duplicados.
    """
    if cliente_id is not None:
        return list(
            cl_models.Contacto.objects
            .filter(
                cliente_id=cliente_id, tipo=tipo, recibe_notificaciones=True
            )
            .values_list("email", flat=True)
        )

    if proyecto_id is None:
        return []

    salida: list[str] = []
    for cliente in clientes(tipo, proyecto_id):
        salida.extend(correos(tipo, cliente_id=cliente["id"]))

    # Un mismo correo puede llegar por dos inversionistas distintos.
    vistos: set[str] = set()
    return [
        e for e in salida
        if e and not (e in vistos or vistos.add(e))
    ]


def clientes(tipo: str, proyecto_id: int) -> list[dict]:
    """Los clientes que son fuente de contacto de ese tipo para el proyecto,
    `[{id, nombre}]`, con la misma regla que `correos`: el puntero de área si
    lo hay, si no los inversionistas VIGENTES.

    Lo usa el Reporte CGM, que le escribe a cada cliente por separado y no a la
    unión de correos. Al portarlo de FastAPI (2026-09-04) esa lista se armó solo
    con el puntero de área: un proyecto sin puntero salía "Sin inversionistas"
    aunque los tuviera, y sus inversionistas no aparecían para enviarles el
    reporte. Una sola regla para los dos usos es lo que evita que se separen.
    """
    puntero = (
        cl_models.ProyectoAreaContacto.objects
        .filter(proyecto_id=proyecto_id, tipo=tipo)
        .values_list("cliente_id", "cliente__razon_social_nombre").first()
    )
    if puntero:
        return [{"id": puntero[0], "nombre": puntero[1]}]

    hoy = date.today()
    inversionistas = (
        py_models.ProyectoInversionista.objects
        .filter(proyecto_id=proyecto_id, cliente_id__isnull=False)
        # Vigente = sin fecha de fin, o con una que aún no pasó.
        .filter(Q(fecha_fin__isnull=True) | Q(fecha_fin__gte=hoy))
        .values_list("cliente_id", "cliente__razon_social_nombre")
    )
    salida: list[dict] = []
    vistos: set[int] = set()
    for cliente_id, nombre in inversionistas:
        if cliente_id in vistos:
            continue
        vistos.add(cliente_id)
        salida.append({"id": cliente_id, "nombre": nombre})
    return salida


def proyecto_ids_por_cliente(tipo: str, cliente_id: int) -> list[int]:
    """Inverso de `correos`: proyectos donde `cliente_id` es la fuente de contacto.

    Por puntero de área, o por ser inversionista vigente en un proyecto que no
    tiene puntero de ese tipo — el puntero manda, igual que en `correos`.
    """
    con_override = cl_models.ProyectoAreaContacto.objects.filter(tipo=tipo)
    override_ids = set(
        con_override.filter(cliente_id=cliente_id).values_list("proyecto_id", flat=True)
    )
    proyectos_con_algun_override = set(
        con_override.values_list("proyecto_id", flat=True)
    )

    hoy = date.today()
    inversionista_ids = {
        pid for pid in py_models.ProyectoInversionista.objects
        .filter(cliente_id=cliente_id)
        .filter(Q(fecha_fin__isnull=True) | Q(fecha_fin__gte=hoy))
        .values_list("proyecto_id", flat=True)
        if pid not in proyectos_con_algun_override
    }

    return list(override_ids | inversionista_ids)
