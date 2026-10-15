"""Los portafolios de TSF de una planta, llevados a los de Operaciones.

«Sincronizar con TSF» trae, además de los datos de la planta, los portafolios
(`investment.Portfolio`) en los que TSF la tiene. Aquí se decide qué cambia:

- **El vínculo es por id de TSF** (`Portafolio.originabot_portfolio_id`), no por
  nombre: TSF repite nombres («Exagon 2» son cuatro portafolios) y aquí `nombre`
  es único. Un portafolio de Operaciones sin vínculo que se llama igual (sin
  contar mayúsculas) se VINCULA en vez de crear otro.
- **Crear un portafolio es opt-in** (`crear=True`): la interfaz lo confirma
  antes, como toda creación que viene de TSF.
- **Solo se tocan los portafolios que vienen de TSF.** Si TSF ya no tiene la
  planta en uno vinculado, se la saca; los portafolios creados en Operaciones
  (sin `originabot_portfolio_id`) no se tocan nunca.
- Si TSF renombró un portafolio vinculado, se renombra aquí, salvo que el nombre
  nuevo choque con otro portafolio.
"""
from __future__ import annotations

from django.db import transaction

from apps.proyectos.models import Portafolio, PortafolioProyecto, Proyecto


def _nombre_libre(nombre: str, portfolio_id: int) -> str:
    """El nombre para crear: el de TSF, o con su id si ya lo usa otro portafolio."""
    if not Portafolio.objects.filter(nombre__iexact=nombre).exists():
        return nombre
    return f"{nombre} (TSF {portfolio_id})"


def plan(proyecto: Proyecto, de_tsf: list[dict]) -> dict:
    """Qué cambiaría, sin escribir. `de_tsf`: `[{id, name}]` de originabot.

    Cada acción lleva lo justo para mostrarla: `portafolio_id` (Operaciones, o
    None si se crearía), `originabot_portfolio_id` y `nombre`.
    """
    ids_tsf = {p["id"] for p in de_tsf}
    vinculados = {
        p.originabot_portfolio_id: p
        for p in Portafolio.objects.filter(originabot_portfolio_id__in=ids_tsf)
    }
    actuales = set(
        PortafolioProyecto.objects.filter(proyecto=proyecto).values_list("portafolio_id", flat=True)
    )

    crear, vincular, renombrar, agregar = [], [], [], []
    for p in sorted(de_tsf, key=lambda x: x["name"].lower()):
        ob_id, nombre = p["id"], p["name"]
        existente = vinculados.get(ob_id)
        if existente is None:
            homonimo = Portafolio.objects.filter(
                nombre__iexact=nombre, originabot_portfolio_id__isnull=True
            ).first()
            if homonimo is not None:
                vincular.append({"portafolio_id": homonimo.id, "originabot_portfolio_id": ob_id, "nombre": homonimo.nombre})
                existente = homonimo
            else:
                crear.append({"portafolio_id": None, "originabot_portfolio_id": ob_id, "nombre": _nombre_libre(nombre, ob_id)})
                continue
        elif existente.nombre != nombre and not Portafolio.objects.filter(
            nombre__iexact=nombre
        ).exclude(pk=existente.pk).exists():
            renombrar.append({
                "portafolio_id": existente.id, "originabot_portfolio_id": ob_id,
                "nombre": nombre, "nombre_anterior": existente.nombre,
            })
        if existente.id not in actuales:
            agregar.append({"portafolio_id": existente.id, "originabot_portfolio_id": ob_id, "nombre": existente.nombre})

    quitar = [
        {"portafolio_id": p.id, "originabot_portfolio_id": p.originabot_portfolio_id, "nombre": p.nombre}
        for p in Portafolio.objects.filter(
            id__in=actuales, originabot_portfolio_id__isnull=False
        ).exclude(originabot_portfolio_id__in=ids_tsf).order_by("nombre")
    ]
    return {
        "de_tsf": [{"id": p["id"], "nombre": p["name"]} for p in de_tsf],
        "crear": crear, "vincular": vincular, "renombrar": renombrar,
        "agregar": agregar, "quitar": quitar,
        "hay_cambios": bool(crear or vincular or renombrar or agregar or quitar),
    }


@transaction.atomic
def aplicar(proyecto: Proyecto, de_tsf: list[dict], crear: bool) -> dict:
    """Aplica `plan()`. Sin `crear`, los portafolios que habría que crear se saltan."""
    cambios = plan(proyecto, de_tsf)
    for v in cambios["vincular"]:
        Portafolio.objects.filter(pk=v["portafolio_id"]).update(
            originabot_portfolio_id=v["originabot_portfolio_id"]
        )
    for r in cambios["renombrar"]:
        Portafolio.objects.filter(pk=r["portafolio_id"]).update(nombre=r["nombre"])
    a_agregar = [a["portafolio_id"] for a in cambios["agregar"]]
    if crear:
        for c in cambios["crear"]:
            nuevo = Portafolio.objects.create(
                nombre=c["nombre"], originabot_portfolio_id=c["originabot_portfolio_id"]
            )
            c["portafolio_id"] = nuevo.id
            a_agregar.append(nuevo.id)
    else:
        cambios["crear"] = []
    PortafolioProyecto.objects.bulk_create(
        [PortafolioProyecto(portafolio_id=pid, proyecto=proyecto) for pid in a_agregar],
        ignore_conflicts=True,
    )
    PortafolioProyecto.objects.filter(
        proyecto=proyecto, portafolio_id__in=[q["portafolio_id"] for q in cambios["quitar"]]
    ).delete()
    return cambios

