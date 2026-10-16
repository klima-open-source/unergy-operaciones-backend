"""Los portafolios de TSF de una planta, llevados a los de Operaciones.

«Sincronizar con TSF» trae, además de los datos de la planta, los portafolios
(`investment.Portfolio`) en los que TSF la tiene. Aquí se decide qué cambia.
Una planta está en UN portafolio (`proyectos.portafolio_id`, Sara, 2026-10-16);
TSF puede tenerla en varios.

- **El vínculo es por id de TSF** (`Portafolio.originabot_portfolio_id`), no por
  nombre: TSF repite nombres («Exagon 2» son cuatro portafolios) y aquí `nombre`
  es único. Un portafolio de Operaciones sin vínculo que se llama igual (sin
  contar mayúsculas) se VINCULA en vez de crear otro.
- **Crear un portafolio es opt-in** (`crear=True`): la interfaz lo confirma
  antes, como toda creación que viene de TSF.
- Si TSF renombró un portafolio vinculado, se renombra aquí, salvo que el nombre
  nuevo choque con otro portafolio.
- **Qué portafolio queda en la planta:**
  - TSF la tiene en UNO: ese, si la planta no tiene portafolio o el que tiene
    viene de TSF. Si está en uno creado en Operaciones, no se mueve: es una
    decisión de alguien de aquí, y se informa en `no_se_mueve`.
  - TSF la tiene en VARIOS: no hay cómo elegir; la planta no se mueve y se
    informa en `varios_en_tsf`. Igual se vinculan, crean y renombran.
  - TSF no la tiene en ninguno: se la saca del suyo solo si ese viene de TSF.

`agregar` y `quitar` llevan a lo sumo un portafolio cada uno; mover es quitar
el viejo y agregar el nuevo.
"""
from __future__ import annotations

from django.db import transaction

from apps.proyectos.models import Portafolio, Proyecto


def _nombre_libre(nombre: str, portfolio_id: int) -> str:
    """El nombre para crear: el de TSF, o con su id si ya lo usa otro portafolio."""
    if not Portafolio.objects.filter(nombre__iexact=nombre).exists():
        return nombre
    return f"{nombre} (TSF {portfolio_id})"


def _accion(p: Portafolio) -> dict:
    return {"portafolio_id": p.id, "originabot_portfolio_id": p.originabot_portfolio_id, "nombre": p.nombre}


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
    actual = proyecto.portafolio

    crear, vincular, renombrar = [], [], []
    # El portafolio de Operaciones de cada uno de TSF (None = se crearía).
    destino: dict[int, Portafolio | None] = {}
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
        elif existente.nombre != nombre and not Portafolio.objects.filter(
            nombre__iexact=nombre
        ).exclude(pk=existente.pk).exists():
            renombrar.append({
                "portafolio_id": existente.id, "originabot_portfolio_id": ob_id,
                "nombre": nombre, "nombre_anterior": existente.nombre,
            })
        destino[ob_id] = existente

    # Un portafolio que se vincula en este plan ya cuenta como de TSF.
    vinculando = {v["portafolio_id"] for v in vincular}
    actual_de_tsf = actual is not None and (
        actual.originabot_portfolio_id is not None or actual.id in vinculando
    )

    agregar, quitar, no_se_mueve, varios_en_tsf = [], [], None, []
    if len(destino) > 1:
        varios_en_tsf = [{"id": p["id"], "nombre": p["name"]} for p in de_tsf]
    elif len(destino) == 1:
        ob_id, nuevo = next(iter(destino.items()))
        ya_esta = nuevo is not None and actual is not None and nuevo.id == actual.id
        if not ya_esta and actual is not None and not actual_de_tsf:
            no_se_mueve = _accion(actual)
        elif not ya_esta:
            if nuevo is None:
                # El mismo dict que `crear`: `aplicar` le pone el id nuevo.
                agregar.append(next(c for c in crear if c["originabot_portfolio_id"] == ob_id))
            else:
                agregar.append({**_accion(nuevo), "originabot_portfolio_id": ob_id})
            if actual is not None:
                quitar.append(_accion(actual))
    elif actual_de_tsf:
        quitar.append(_accion(actual))

    return {
        "de_tsf": [{"id": p["id"], "nombre": p["name"]} for p in de_tsf],
        "crear": crear, "vincular": vincular, "renombrar": renombrar,
        "agregar": agregar, "quitar": quitar,
        "no_se_mueve": no_se_mueve, "varios_en_tsf": varios_en_tsf,
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
    if crear:
        for c in cambios["crear"]:
            c["portafolio_id"] = Portafolio.objects.create(
                nombre=c["nombre"], originabot_portfolio_id=c["originabot_portfolio_id"]
            ).id
    else:
        cambios["crear"] = []
        # Sin crear no hay a dónde mover: la planta se queda donde estaba.
        if any(a["portafolio_id"] is None for a in cambios["agregar"]):
            cambios["agregar"], cambios["quitar"] = [], []

    if cambios["agregar"] or cambios["quitar"]:
        proyecto.portafolio_id = cambios["agregar"][0]["portafolio_id"] if cambios["agregar"] else None
        proyecto.save(update_fields=["portafolio"])
    return cambios
