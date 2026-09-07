"""Elimina `mantenimientos` — 0 filas y ninguna forma de crear un registro.

La tabla registraba el mantenimiento de una planta (preventivo / correctivo /
predictivo) con fecha, descripcion y estado. Se elimina por una razon distinta
a la de `mantenimiento_impacto` (migracion 0005): ahi habia codigo sin
consumidor, aca habia un consumidor sin fuente.

**Nada podia crear un mantenimiento.** Verificado el 2026-09-07 en los tres
lados: ningun endpoint en Django ni en el arbol FastAPI, ninguna pantalla en
`../unergy-operaciones-frontend` (la palabra "preventivo" no aparece en ningun
archivo), y ni un solo `Mantenimiento.objects.create` en el repo. La unica
lectura era `api/v1/monitoreo/queryset.py::build_fmo`.

El consumidor era la **seccion 5 del informe mensual de FMO** ("PLAN DE
MANTENIMIENTO EJECUTADO — Anexo 3"), que se le manda al cliente. **No se rompe**:
el frontend ya resolvia la clave ausente como lista vacia
(`fmoData?.mantenimientos || []`), asi que la seccion imprime el mismo aviso de
"Sin registros de mantenimiento en el periodo." que imprimia con la tabla vacia.
Su tipo TypeScript es un indice abierto, y los informes ya enviados guardan su
HTML congelado en `informes.html_content` — `html_para_enviar()` no reconsulta
nada.

Queda dicho para quien lo retome: la tabla vacia no era un descuido, era el
sintoma. Falta la entidad **equipo** de la que deberia colgar el mantenimiento
— hoy el activo fisico tiene cuatro representaciones parciales y ninguna es un
inventario (ver §4 de `docs/ARQUITECTURA_MONITOREO.md`). Por eso no se le
construyo un CRUD: registrar mantenimientos por PLANTA no responde la pregunta
del negocio, que es "que equipo tiene mantenimiento pendiente". El refactor lo
reemplaza por `equipo_mantenimientos`, colgado del equipo y con
`mantenimiento_intervalo_dias` para poder cruzar el intervalo contra el ultimo
ejecutado: ver `docs/refactor/02-modelo.md` §2.3 y el paso 7.2 de
`docs/refactor/06-plan-migracion.md`.

El DDL original esta en `alembic/versions/` (`git log` sobre esa carpeta).
Ninguna otra tabla tiene FK **hacia** esta.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('monitoreo', '0005_eliminar_mantenimiento_impacto'),
    ]

    operations = [
        migrations.DeleteModel(
            name='Mantenimiento',
        ),
    ]
