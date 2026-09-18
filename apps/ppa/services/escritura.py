"""La ÚNICA forma de crear un contrato PPA.

Hasta el 2026-09-17 había dos, y aplicaban reglas distintas sobre la misma
tabla: `POST /ppa` (el wizard de Servicios, que creó los 35 contratos de
producción) validaba contra GESCON y sincronizaba las partes; `firmar()` del CRM
no hacía ninguna de las dos, pero sí escribía las tarifas. Un contrato quedaba
con unas reglas u otras según por dónde hubiera entrado. Ver
`docs/DIAGNOSTICO_PPA.md` §2.

Acá viven las reglas, una sola vez. Los dos caminos arman `datos` y llaman a
`crear_ppa()`.

## Las tres decisiones que fija esta función

**El tipo de contrato es un PARÁMETRO OBLIGATORIO y nunca se adivina.** `firmar()`
grababa `"compra"` a mano, lo cual funciona solo porque el CRM todavía no tiene
ofertas de venta. El día que las tenga, adivinar grabaría al cliente del lado
equivocado —vendedor cuando es comprador— y el error no falla de forma visible:
llega hasta la facturación con las partes invertidas. Por eso `TIPOS_CONTRATO` es
una guarda que revienta con un tipo desconocido, en vez de dejar pasar un
default. `DOMINIO_COMERCIAL.md` lo llama «el punto más delicado de toda la
etapa».

**Un contrato sin plantas se crea, pero AVISA.** Es un caso legítimo —la planta
puede no existir todavía como `Proyecto`— y por eso no se bloquea. Pero sin
plantas Cumplimiento no tiene contra qué medir, y así es como quedaron 11 de los
33 contratos vivos: el paso del wizard dice «(opcional)» y nadie mira. El aviso
viaja en el resultado para que la UI lo muestre; decidir si se puede seguir es
de quien crea el contrato, no de esta función.

**Todo o nada.** Contrato, plantas, tarifas y compromisos en UNA transacción. El
wizard hacía tres peticiones seguidas sin transacción común, y por eso hay 13
contratos sin tarifas y 14 sin compromisos: si la segunda petición fallaba, el
contrato quedaba a medias y nadie se enteraba.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction

from apps.clientes.services import documentos as documentos_service
from apps.ppa import models as ppa_models
from apps.ppa.services import contratos as contratos_service

NOMBRE_ENLACE = "Enlace Drive del contrato"

# Los dos lados del negocio. El default de la columna es "venta" y seis módulos
# de Cumplimiento leen `(tipo or "venta") == "compra"`, así que «ausente» ya
# significa venta en todo el sistema. Acá no se admite ausente: se dice cuál es.
TIPOS_CONTRATO = ("compra", "venta")


@dataclass
class Resultado:
    """Lo que se creó, y lo que quedó cojo.

    `avisos` no son errores: el contrato existe. Son las cosas que impiden que
    Cumplimiento o Facturación puedan trabajar con él, y que la UI tiene que
    poner delante del usuario en vez de dejarlas pasar en silencio.
    """

    contrato: ppa_models.PpaContrato
    plantas: int = 0
    tarifas: int = 0
    compromisos: int = 0
    avisos: list[str] = field(default_factory=list)


def _avisos(plantas: int, tarifas: int, compromisos: int) -> list[str]:
    salida = []
    if not plantas:
        salida.append(
            "El contrato quedó sin plantas vinculadas: Cumplimiento no puede "
            "medirlo hasta que se le asocie al menos una."
        )
    if not tarifas:
        salida.append(
            "El contrato quedó sin tabla de tarifas: no se puede facturar por "
            "tarifa indexada."
        )
    if not compromisos:
        salida.append(
            "El contrato quedó sin compromisos de energía: no hay mínimo contra "
            "el cual medir la generación."
        )
    return salida


def crear_ppa(
    *,
    tipo_contrato: str,
    datos: dict,
    proyecto_ids: list[int] | None = None,
    tarifas: list[dict] | None = None,
    compromisos: list[dict] | None = None,
    carpeta_link: str | None = None,
) -> Resultado:
    """Crea un PPA completo. Todo en una transacción, o nada.

    `datos` son las columnas de `ppa_contratos` ya validadas por quien llama
    —el serializer de la API o el del CRM—; esta función no valida formatos,
    valida REGLAS.

    Lanza `contratos_service.ReglaPpa` (→ 422) si la fecha de fin queda antes
    que la de algún registro GESCON de sus plantas, y `ValueError` si el tipo de
    contrato no es uno de los dos conocidos.
    """
    if tipo_contrato not in TIPOS_CONTRATO:
        raise ValueError(
            f'tipo_contrato inválido: {tipo_contrato!r}. '
            f"Esperado uno de {TIPOS_CONTRATO}. No se adivina: grabar el tipo "
            "equivocado invierte comprador y vendedor, y eso llega hasta la "
            "facturación sin fallar de forma visible."
        )

    proyecto_ids = list(proyecto_ids or [])
    tarifas = list(tarifas or [])
    compromisos = list(compromisos or [])

    with transaction.atomic():
        contrato = ppa_models.PpaContrato.objects.create(
            tipo_contrato=tipo_contrato, **datos
        )
        contratos_service.fijar_proyectos(contrato, proyecto_ids)
        contratos_service.sincronizar_partes(contrato)

        if tarifas:
            ppa_models.PpaTarifa.objects.bulk_create([
                ppa_models.PpaTarifa(contrato=contrato, **fila) for fila in tarifas
            ])
        if compromisos:
            ppa_models.PpaCompromisoEnergia.objects.bulk_create([
                ppa_models.PpaCompromisoEnergia(contrato=contrato, **fila)
                for fila in compromisos
            ])
        if carpeta_link:
            documentos_service.set_enlace(
                ppa_contrato_id=contrato.id, url=carpeta_link, nombre=NOMBRE_ENLACE
            )

        # Al final y DENTRO de la transacción: la regla mira los registros GESCON
        # de las plantas, así que necesita que ya estén vinculadas, y si falla
        # tiene que deshacer el contrato entero.
        contratos_service.validar_fecha_fin_vs_asic(contrato)

    return Resultado(
        contrato=contrato,
        plantas=len(proyecto_ids),
        tarifas=len(tarifas),
        compromisos=len(compromisos),
        avisos=_avisos(len(proyecto_ids), len(tarifas), len(compromisos)),
    )
