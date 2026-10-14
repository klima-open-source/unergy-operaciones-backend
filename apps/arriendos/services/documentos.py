"""Documentos de arriendo: subida, copias por predio y descarga.

Dos formas de subir, y la diferencia importa:

- **`subir`**: un documento para UN pago. Se guarda tal cual.
- **`subir_cuenta_cobro`**: UN archivo (cuenta de cobro o factura) que cubre
  VARIOS predios. Se escribe una copia renombrada por predio —el nombre lo pide
  contabilidad: `[PREDIO]_[YYYY-MM]_[Arrendatario]_[Proyecto].pdf`— y se crea una
  fila por cada uno. El original se conserva UNA sola vez como referencia.
"""

import json
from pathlib import Path

from django.conf import settings
from django.db import transaction

from apps.arriendos import models as ar_models
from apps.proyectos import models as py_models

PROHIBIDOS = '/\\:*?"<>|'


def directorio() -> Path:
    return Path(settings.BASE_DIR) / "uploads" / "arriendos"


def segmento_seguro(nombre: str) -> str:
    """Sanea un componente de ruta: sin separadores ni `..`.

    La ruta se arma con datos del cliente (`codigo_contrato`), así que sin esto
    un `../` escribiría fuera del directorio de subidas.
    """
    limpio = (
        "".join(c for c in (nombre or "") if c not in PROHIBIDOS)
        .replace("..", "").strip()
    )
    return limpio or "sin_codigo"


def nombre_seguro(nombre: str, respaldo: str) -> str:
    limpio = "".join(c for c in (nombre or "") if c not in PROHIBIDOS).strip()
    return limpio or respaldo


def carpeta(periodo: str, codigo_contrato: str) -> Path:
    destino = directorio() / periodo / segmento_seguro(codigo_contrato)
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def escribir(destino: Path, nombre: str, contenido: bytes) -> Path:
    ruta = destino / nombre
    ruta.write_bytes(contenido)
    return ruta


@transaction.atomic
def subir(datos: dict, principal, secundario=None) -> object:
    """Un documento para un pago. Upsert por (proyecto, período, pago)."""
    destino = carpeta(datos["periodo"], datos["codigo_contrato"])

    extension = Path(principal.name or "doc.pdf").suffix or ".pdf"
    nombre = datos["nombre_resultante"]
    if not nombre.endswith(extension):
        nombre += extension
    ruta = escribir(destino, nombre, principal.read())

    nombre_sec = ruta_sec = None
    if secundario is not None and secundario.name:
        ext_sec = Path(secundario.name).suffix or ".pdf"
        nombre_sec = f'{nombre.rsplit(".", 1)[0]}_enviada{ext_sec}'
        ruta_sec = str(escribir(destino, nombre_sec, secundario.read()))

    valores = {
        "codigo_contrato": datos["codigo_contrato"],
        "tipo_documento": datos["tipo_documento"],
        "nombre_archivo": nombre,
        "ruta_local": str(ruta),
        "nombre_secundario": nombre_sec,
        "ruta_secundario": ruta_sec,
    }
    if datos.get("proyecto_id") is not None:
        valores["proyecto_id"] = datos["proyecto_id"]

    documento, _ = ar_models.ArrDocumento.objects.update_or_create(
        arr_proyecto_id=datos["arr_proyecto_id"],
        periodo=datos["periodo"],
        pago_id=datos["pago_id"],
        defaults=valores,
    )
    return documento


def _mapa_arr_a_proyecto() -> dict[int, int]:
    """Match difuso `ArrProyecto` → `Proyecto`, calculado una sola vez.

    Misma lógica del seed de O&M: ignora el código MGS y compara tokens.
    """
    from apps.om.services.calculadora import om_keys, om_match_seed

    activos = list(ar_models.ArrProyecto.objects.filter(activo=True))
    claves = [(a, om_keys(a.nombre)) for a in activos]
    mapa: dict[int, int] = {}
    for proyecto in py_models.Proyecto.objects.all():
        encontrado = om_match_seed(proyecto.nombre_comercial or "", claves)
        if encontrado is not None:
            mapa[encontrado.id] = proyecto.id
    return mapa


def _entero(valor):
    try:
        return int(valor) if valor is not None else None
    except (TypeError, ValueError):
        return None


@transaction.atomic
def subir_cuenta_cobro(datos: dict, principal, secundario=None) -> dict:
    """Un archivo, una copia renombrada por predio.

    Los predios SIN match (`arr_proyecto_id` nulo) también se guardan, para
    revisión manual: perderlos dejaría pagos sin soporte.
    """
    predios = json.loads(datos["predios"])
    if not isinstance(predios, list) or not predios:
        raise ValueError("predios debe ser un JSON array no vacío")

    periodo, pago_id = datos["periodo"], datos["pago_id"]
    destino = carpeta(periodo, datos["codigo_contrato"])
    contenido = principal.read()

    # El original se conserva una sola vez, sin renombrar.
    ext = Path(principal.name or "documento.pdf").suffix or ".pdf"
    ruta_original = escribir(
        destino, f"_original_pago{pago_id}{ext}", contenido
    )

    nombre_sec = ruta_sec = None
    if secundario is not None and secundario.name:
        ext_sec = Path(secundario.name).suffix or ".pdf"
        nombre_sec = f"_enviada_pago{pago_id}{ext_sec}"
        ruta_sec = str(escribir(destino, nombre_sec, secundario.read()))

    mapa: dict | None = None
    asociados = sin_match = 0

    for predio in predios:
        arr_proyecto_id = _entero(predio.get("arr_proyecto_id"))
        arr_arrendador_id = _entero(predio.get("arr_arrendador_id"))
        proyecto_id = _entero(predio.get("proyecto_id"))
        if proyecto_id is None and arr_proyecto_id is not None:
            if mapa is None:
                mapa = _mapa_arr_a_proyecto()
            proyecto_id = mapa.get(arr_proyecto_id)

        nombre = nombre_seguro(
            str(predio.get("nombre_resultante") or _nombre_de_respaldo(
                predio, periodo, datos.get("nombre_arrendatario"),
                proyecto_id, arr_proyecto_id,
            )),
            f"documento_pago{pago_id}.pdf",
        )
        ruta_copia = escribir(destino, nombre, contenido)

        documento = _documento_del_predio(
            arr_proyecto_id, arr_arrendador_id, periodo, pago_id
        )
        documento.arr_arrendador_id = arr_arrendador_id
        if proyecto_id is not None:
            documento.proyecto_id = proyecto_id
        documento.codigo_contrato = datos["codigo_contrato"]
        documento.tipo_documento = datos["tipo_documento"]
        documento.nombre_archivo = nombre
        documento.ruta_local = str(ruta_copia)
        documento.ruta_original = str(ruta_original)
        documento.nombre_secundario = nombre_sec
        documento.ruta_secundario = ruta_sec
        documento.codigo_predio = predio.get("codigo_predio")
        documento.numero_cuenta_cobro = datos.get("numero_cuenta_cobro")
        documento.nombre_arrendatario = datos.get("nombre_arrendatario")
        documento.valor_individual = predio.get("valor_individual")
        documento.save()

        if arr_proyecto_id is not None:
            asociados += 1
        else:
            sin_match += 1

    return {
        "ok": True,
        "predios_asociados": asociados,
        "predios_sin_match": sin_match,
        "copias_generadas": asociados + sin_match,
    }


def _documento_del_predio(arr_proyecto_id, arr_arrendador_id, periodo, pago_id):
    """Con match, upsert; sin match, siempre una fila nueva."""
    if arr_proyecto_id is None:
        return ar_models.ArrDocumento(
            arr_proyecto_id=None, arr_arrendador_id=arr_arrendador_id,
            periodo=periodo, pago_id=pago_id,
        )
    consulta = ar_models.ArrDocumento.objects.filter(
        arr_proyecto_id=arr_proyecto_id, periodo=periodo, pago_id=pago_id
    )
    if arr_arrendador_id is not None:
        consulta = consulta.filter(arr_arrendador_id=arr_arrendador_id)
    return consulta.first() or ar_models.ArrDocumento(
        arr_proyecto_id=arr_proyecto_id, arr_arrendador_id=arr_arrendador_id,
        periodo=periodo, pago_id=pago_id,
    )


def _nombre_de_respaldo(predio, periodo, arrendatario, proyecto_id, arr_id) -> str:
    """`[PREDIO]_[YYYY-MM]_[Arrendatario]_[Proyecto].pdf` armado desde la base.

    Solo se usa si el frontend no mandó `nombre_resultante`.
    """
    nombre_proyecto = None
    if proyecto_id is not None:
        nombre_proyecto = (
            py_models.Proyecto.objects.filter(pk=proyecto_id)
            .values_list("nombre_comercial", flat=True).first()
        )
    elif arr_id is not None:
        nombre_proyecto = (
            ar_models.ArrProyecto.objects.filter(pk=arr_id)
            .values_list("nombre", flat=True).first()
        )
    partes = [predio.get("codigo_predio") or "predio", periodo]
    if arrendatario:
        partes.append(arrendatario)
    partes.append(nombre_proyecto or "SIN-MATCH")
    return "_".join(partes) + ".pdf"


def ruta_de_descarga(documento, secundario: bool) -> Path:
    """La ruta a servir, validada contra el directorio de subidas."""
    cruda = documento.ruta_secundario if secundario else documento.ruta_local
    if not cruda:
        raise FileNotFoundError("Archivo no disponible")
    ruta = Path(cruda).resolve()
    # La ruta sale de la base, pero se comprueba que siga DENTRO del directorio
    # de subidas: un valor manipulado no debe servir cualquier archivo.
    if not str(ruta).startswith(str(directorio().resolve())):
        raise PermissionError("Acceso denegado")
    if not ruta.exists():
        raise FileNotFoundError("Archivo no encontrado en el servidor")
    return ruta
