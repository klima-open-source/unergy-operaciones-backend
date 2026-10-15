"""Diagnóstico de la migración `ArrProyecto` → contrato de arriendo.

Solo lectura: dimensiona cuánto falta por migrar. Empareja cada `ArrProyecto`
con su `Proyecto` de forma DIFUSA (la misma lógica del seed de O&M: ignora el
código MGS y compara tokens). No modifica nada.
"""

from apps.arriendos import models as ar_models
from apps.contratos import models as ct_models
from apps.proyectos import models as py_models

MUESTRA = 30


def diagnostico() -> dict:
    from apps.om.services.calculadora import om_keys, om_match_seed

    activos = list(ar_models.ArrProyecto.objects.filter(activo=True))
    claves = [(a, om_keys(a.nombre)) for a in activos]
    con_contrato_arr = set(
        ct_models.ContratoServicio.objects
        .filter(servicio_aplica="arriendo", proyectos_vinculados__isnull=False)
        .values_list("proyectos_vinculados__proyecto_id", flat=True)
    )

    emparejados: set[int] = set()
    con_contrato, sin_contrato = 0, []
    for proyecto in py_models.Proyecto.objects.all():
        encontrado = om_match_seed(proyecto.nombre_comercial or "", claves)
        # Un `ArrProyecto` solo puede emparejarse UNA vez: si dos proyectos
        # casan con el mismo, el segundo se descarta en vez de contarlo doble.
        if encontrado is None or encontrado.id in emparejados:
            continue
        emparejados.add(encontrado.id)
        if proyecto.id in con_contrato_arr:
            con_contrato += 1
        else:
            sin_contrato.append(
                f"{encontrado.nombre} → {proyecto.nombre_comercial}"
            )

    sin_match = [a.nombre for a in activos if a.id not in emparejados]
    return {
        "total_arr_proyectos": len(activos),
        "con_contrato_arriendo": con_contrato,
        "sin_contrato_arriendo": len(sin_contrato),
        "sin_match_de_proyecto": len(sin_match),
        "ejemplos_sin_contrato": sin_contrato[:MUESTRA],
        "ejemplos_sin_match": sin_match[:MUESTRA],
    }
