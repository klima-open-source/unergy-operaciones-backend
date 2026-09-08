"""Los serializers de escritura del proxy usan los nombres de campo de la API.

Bug real (2026-09-08): guardar los códigos SIC en «IDs de Proyectos» no hacía
nada. Un PATCH pasa por dos filtros —el serializer de la vista y la lista de
campos permitidos del cliente— y la migración a Django los dejó con nombres
distintos, así que el campo se caía entre las dos capas:

    la vista aceptaba      codigo_sic, codigo_frt, es_generador, es_comercializador
    el cliente dejaba pasar sic_gen, sic_con, frt_gen, frt_con, from_generator, …

De los 7 campos actualizables del proyecto solo servía `ac_power`. La pantalla
manda `{sic_gen, sic_con}` y recibía 400 «No se enviaron campos para
actualizar»; con los nombres del serializer recibía 502 «No hay campos válidos
para actualizar». No había payload que funcionara.

En subproyectos era peor porque es silencioso: de los tres ids de Quoia solo
llegaba `quoia_node_id`, y los dos de reporte se descartaban sin error.

Los esquemas de Pydantic de FastAPI sí usaban los nombres de la API; se
perdieron al reescribir los serializers.

Las 2 722 pruebas pasaban con esto roto porque ninguna cruza las dos capas:
el serializer se prueba solo y el cliente se prueba solo. Este test las cruza.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _django_listo():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _casos():
    """(ruta, serializer de la vista, campos que el cliente deja pasar)."""
    from api.v1.liquidaciones_proxy import serializers as s
    from apps.liquidaciones.services import api_externa as api

    return [
        ("PATCH /liquidaciones-api/proyectos/{id}",
         s.ProyectoUpdateSerializer, api.CAMPOS_PROYECTO),
        ("PATCH /liquidaciones-api/subproyectos/{topico}",
         s.SubproyectoUpdateSerializer, api.CAMPOS_QUOIA),
    ]


def test_ningun_campo_se_cae_entre_la_vista_y_el_cliente():
    """Todo lo que el serializer acepta tiene que llegar a la API externa.

    Un campo declarado que el cliente no reconoce se descarta sin avisar: el
    usuario guarda, recibe 200 y no se escribió nada.
    """
    for ruta, serializer, permitidos in _casos():
        declarados = set(serializer().get_fields())
        perdidos = declarados - set(permitidos)
        assert not perdidos, (
            f"{ruta}: el serializer acepta {sorted(perdidos)}, que el cliente "
            f"descarta. Los nombres de la API son {sorted(permitidos)}."
        )


def test_todo_campo_actualizable_se_puede_mandar():
    """Y al revés: un campo que la API acepta no puede quedar inalcanzable.

    Si el serializer no lo declara, DRF lo borra del `validated_data` y no hay
    forma de tocarlo desde la plataforma.
    """
    for ruta, serializer, permitidos in _casos():
        declarados = set(serializer().get_fields())
        inalcanzables = set(permitidos) - declarados
        assert not inalcanzables, (
            f"{ruta}: {sorted(inalcanzables)} no se pueden actualizar porque el "
            f"serializer no los declara."
        )
