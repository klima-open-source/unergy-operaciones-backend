"""Elimina `mantenimiento_impacto` — 0 filas y ningun consumidor.

La tabla guardaba, por evento de intervencion, la energia que la planta dejo de
entregar (`kwh_p90` esperado - `kwh_real`) y su costo a la tarifa PPA del mes.
La idea era buena; lo que nunca existio fue el consumidor. Verificado el
2026-09-07 contra `../unergy-operaciones-frontend`:

  * `generar_impacto` (la bandera del `POST /fallas` que creaba la fila): 0
    apariciones en el frontend. Siempre viajaba en `false`.
  * los 5 endpoints de `/api/v1/mantenimiento-impacto`: 0 apariciones.
  * `energia_perdida_mantenimiento_mwh`, `gen_disponible_mwh`,
    `compras_bolsa_ajustada_mwh` y `riesgo_penalizacion_mantenimiento`, que
    `cumplimiento/resumen.py` derivaba de aca: ningun componente los leia. El
    frontend calcula su propia disponibilidad por otro camino.

Y el dato tampoco estaba: 0 filas. `kwh_p90` (la energia esperada) no la llena
ningun job, y el calculo corria una sola vez **al crear** la falla, cuando aun
no esta resuelta — la ventana cerraba en `now()`, se convertia a dias y
consultaba la `generacion_diaria` de HOY, que todavia no se ha cargado. Salia
`lost_energy_kwh = None` y el informe la descartaba con su propio filtro
`lost_energy_kwh__isnull=False`. No habia recalculo en ninguna parte.

Consistente con el plan del refactor, que ya la tenia marcada: ver
`docs/refactor/04-mapeo.md` (§ "se elimina -> falla_impactos") y el paso 7.2 de
`docs/refactor/06-plan-migracion.md`.

**Si se quiere reponer**, el trabajo no es la bandera: es (1) recalcular al
RESOLVER la falla o con una tarea programada, no al crearla, (2) definir quien
crea las filas, y (3) resolver los dos calculos rivales de "impacto" — el que la
UI muestra hoy es la regla de dedo de `fallas/dominio.py::estimar_perdida`
(placa x 0.18 x horas), no este. El DDL original esta en
`alembic/versions/034_maintenance_impact.py`.

Ninguna otra tabla tiene FK **hacia** esta. Django emite
`DROP TABLE "mantenimiento_impacto" CASCADE` (es su forma estandar, no una
decision de esta migracion): sin dependientes, el CASCADE no arrastra nada.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('monitoreo', '0004_orden_historial_seguimientos'),
    ]

    operations = [
        migrations.DeleteModel(
            name='MantenimientoImpacto',
        ),
    ]
