"""Los equipos de la planta y la traducción desde la vieja «info técnica».

Modelo de Sara (2026-10-15): `proyecto_info_tecnica` (una fila por planta con la
marca de cada tipo de equipo) se reparte en

- `proyectos.capacidad_instalada_kwp` y `proyectos.voltaje_red`;
- `equipos`: una fila por tipo (tipo, cantidad, marca, potencia de UNA unidad,
  detalle);
- `inversores` (`ProyectoInversor`): cada inversor con su potencia, colgado del
  equipo de tipo inversor.

`MAPA` traduce cada campo viejo a su lugar nuevo. Lo usan el comando que copia los
datos, el `PUT /proyectos/{id}/info-tecnica` (para el frontend que todavía lo
llama) y `ficha_tecnica`, que arma el objeto viejo para leerlo igual que antes.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db import transaction

from apps.proyectos.models import Equipo, Proyecto, ProyectoInversor, TipoEquipo

T = TipoEquipo

# campo viejo -> (tipo de equipo, atributo del equipo)
MAPA: dict[str, tuple[str, str]] = {
    "cantidad_total_paneles": (T.PANEL, "cantidad"),
    "potencia_panel_kwp": (T.PANEL, "potencia"),
    "marca_paneles": (T.PANEL, "marca"),
    "cantidad_inversores": (T.INVERSOR, "cantidad"),
    "marca_inversores": (T.INVERSOR, "marca"),
    "cantidad_strings": (T.INVERSOR, "detalle"),
    "marca_transformador": (T.TRANSFORMADOR, "marca"),
    "marca_reconectador_rele": (T.RECONECTADOR, "marca"),
    "marca_totalizador": (T.TOTALIZADOR, "marca"),
    "marca_seguidor_solar": (T.SEGUIDOR, "marca"),
    "tipo_tracker": (T.SEGUIDOR, "detalle"),
    "marca_medidores_frontera": (T.MEDIDOR_FRONTERA, "marca"),
    "marca_modem_reconectador": (T.MODEM_RECONECTADOR, "marca"),
    "ip_modem_reconectador": (T.MODEM_RECONECTADOR, "detalle"),
    "marca_modems_frontera": (T.MODEM_FRONTERA, "marca"),
    "marca_cctv": (T.CCTV, "marca"),
    "cctv_estado": (T.CCTV, "detalle"),
    "seguridad_fisica": (T.SEGURIDAD, "detalle"),
}
DEL_PROYECTO = ("capacidad_instalada_kwp", "voltaje_red")
# Ya no se guardan: la potencia de los inversores es la de cada uno (el texto
# "300, 60 y 36" no coincidía con ellos), el internet sale del contrato y la
# ubicación de latitud/longitud.
DESCARTADOS = ("potencia_inversores_kwp", "tiene_internet", "url_ubicacion")

PREFIJO_STRINGS = "Strings: "


def a_decimal(valor) -> Decimal | None:
    """`"0,57"`, `"0.57"`, `0.57` -> Decimal; `None` si no es un número."""
    if valor in (None, ""):
        return None
    try:
        return Decimal(str(valor).strip().replace(",", "."))
    except InvalidOperation:
        return None


def _vacio(valor) -> bool:
    return valor is None or (isinstance(valor, str) and not valor.strip())


def _a_texto_del_equipo(campo: str, valor):
    if campo == "cantidad_strings":
        return f"{PREFIJO_STRINGS}{valor}"
    return valor


def _de_texto_del_equipo(campo: str, valor):
    if campo == "cantidad_strings":
        if not valor or not str(valor).startswith(PREFIJO_STRINGS):
            return None
        numero = str(valor)[len(PREFIJO_STRINGS):].strip()
        return int(numero) if numero.isdigit() else None
    return valor


@transaction.atomic
def guardar_ficha_tecnica(proyecto: Proyecto, datos: dict) -> list[str]:
    """Aplica campos con la forma de la vieja info técnica. Solo toca los que vienen.

    Devuelve avisos de lo que no se pudo guardar (p. ej. una potencia de panel que
    no es un número), sin cortar el resto.
    """
    avisos: list[str] = []
    cambios_proyecto = [c for c in DEL_PROYECTO if c in datos]
    for campo in cambios_proyecto:
        setattr(proyecto, campo, datos[campo])
    if cambios_proyecto:
        proyecto.save(update_fields=cambios_proyecto)

    equipos = {e.tipo: e for e in Equipo.objects.filter(proyecto=proyecto)}
    tiene_inversores = ProyectoInversor.objects.filter(proyecto=proyecto).exists()
    for campo, (tipo, atributo) in MAPA.items():
        if campo not in datos:
            continue
        # Con inversores cargados, su cantidad es la de ellos (la mantiene
        # `actualizar_cantidad_inversores`), no la que se escriba aquí.
        if campo == "cantidad_inversores" and tiene_inversores:
            continue
        valor = datos[campo]
        if atributo == "potencia" and not _vacio(valor):
            numero = a_decimal(valor)
            if numero is None:
                avisos.append(f"{campo}: «{valor}» no es un número; no se guardó")
                continue
            valor = numero
        elif atributo == "detalle" and not _vacio(valor):
            valor = _a_texto_del_equipo(campo, valor)
        equipo = equipos.get(tipo)
        if equipo is None:
            if _vacio(valor):
                continue
            equipo = equipos[tipo] = Equipo(proyecto=proyecto, tipo=tipo)
        setattr(equipo, atributo, None if _vacio(valor) else valor)
        equipo.save()
    return avisos


def ficha_tecnica(proyecto: Proyecto) -> dict | None:
    """La info técnica con la forma vieja, armada desde `proyectos` y `equipos`.

    Usa `proyecto.equipos.all()`: con prefetch no
    cuesta consultas. `None` si la planta no tiene nada técnico cargado.
    """
    equipos = {e.tipo: e for e in proyecto.equipos.all()}
    ficha: dict = {c: getattr(proyecto, c) for c in DEL_PROYECTO}
    for campo, (tipo, atributo) in MAPA.items():
        equipo = equipos.get(tipo)
        valor = getattr(equipo, atributo) if equipo else None
        ficha[campo] = _de_texto_del_equipo(campo, valor) if atributo == "detalle" else valor
    for campo in DESCARTADOS:
        ficha[campo] = None
    if all(v is None for v in ficha.values()):
        return None
    for campo in ("capacidad_instalada_kwp", "potencia_panel_kwp"):
        if ficha[campo] is not None:
            ficha[campo] = float(ficha[campo])
    ficha["proyecto_id"] = proyecto.id
    ficha["potencia_ac_kw"] = float(proyecto.potencia_ac_kw) if proyecto.potencia_ac_kw is not None else None
    return ficha


def equipo_de_inversores(proyecto_id: int) -> Equipo:
    """El equipo de tipo inversor de la planta; lo crea si no existe."""
    equipo, _ = Equipo.objects.get_or_create(proyecto_id=proyecto_id, tipo=T.INVERSOR)
    return equipo


def actualizar_cantidad_inversores(proyecto_id: int) -> None:
    """La cantidad del equipo de inversores = los inversores activos de la planta."""
    activos = ProyectoInversor.objects.filter(proyecto_id=proyecto_id, estado="activo").count()
    Equipo.objects.filter(proyecto_id=proyecto_id, tipo=T.INVERSOR).update(cantidad=activos or None)
