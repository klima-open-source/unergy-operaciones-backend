"""Elimina `pagos_servicio`: nunca se registró un pago.

La tabla nació con la revisión 011 de Alembic (2026-05-25), la misma que creó
los servicios de Operación. Traía `unique_together` por contrato/mes/año y
`CHECK` de rango en los dos campos, así que alguien la diseñó con cuidado.

Nunca se usó. Verificado contra producción el 2026-09-17:

- **0 filas.**
- **0 cambios** en `audit_log`, sobre 147.102 registros de mayo a septiembre.
- **Ninguna tabla la referencia** por clave foránea.

Y sí tenía todo alrededor: dos endpoints REST completos (listar, crear, editar,
borrar, con filtros por año y mes), cuatro métodos en el servicio del front, y
tablas de pagos en `OperacionView.vue` para mantenimiento y arriendo. Lo único
que nunca tuvo fue un registro -- ni pruebas.

Es el mismo caso que `servicio_operacion` y `servicio_representacion`, que la
revisión 118 borró por la misma razón: *"tablas de diseño temprano… 0 filas en
producción las dos"*. Confirmado con el equipo de operaciones antes de borrarla.

**Se pierde la estructura, no datos**: la tabla está vacía. Si mañana hace falta
registrar pagos, esta migración y el commit que la acompaña dicen exactamente
cómo estaba hecha.

`DROP TABLE` es irreversible. La reversa recrea la tabla vacía con su forma
original --las dos `CHECK` y el índice incluidos-- para poder volver atrás si el
despliegue falla por otra cosa; no recupera filas porque no había ninguna.
"""

from django.db import migrations

RECREAR = """
CREATE TABLE IF NOT EXISTS pagos_servicio (
    id BIGSERIAL PRIMARY KEY,
    contrato_id BIGINT NOT NULL
        REFERENCES contratos_servicio(id) ON DELETE CASCADE,
    mes INTEGER NOT NULL,
    año INTEGER NOT NULL,
    valor_pagado NUMERIC(14, 2),
    estado estado_pago_enum NOT NULL DEFAULT 'pendiente',
    enlace_factura VARCHAR(1000),
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT ck_pago_servicio_mes_rango CHECK (mes >= 1 AND mes <= 12),
    CONSTRAINT ck_pago_servicio_ano_rango CHECK (año >= 2020 AND año <= 2099),
    CONSTRAINT uq_pago_servicio_contrato_periodo UNIQUE (contrato_id, mes, año)
);
CREATE INDEX IF NOT EXISTS ix_pagos_servicio_contrato_id
    ON pagos_servicio (contrato_id);
"""


class Migration(migrations.Migration):

    dependencies = [("contratos", "0006_estado_contrato_decision_humana")]

    operations = [
        # Le quita el modelo al estado de Django sin tocar la base: el DROP va
        # aparte, para que el SQL diga exactamente lo que pasa.
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.DeleteModel(name="PagoServicio")],
        ),
        migrations.RunSQL(
            sql="DROP TABLE IF EXISTS pagos_servicio;",
            reverse_sql=RECREAR,
        ),
    ]
