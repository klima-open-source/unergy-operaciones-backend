"""`contratos_servicio.estado` pasa a ser solo lo que una persona decide.

    vigente | vencido | terminado | en_renovacion
        ->  firmado | en_renovacion | terminado

`vigente` y `vencido` NO eran decisiones: son consecuencia de `fecha_fin`, y
nadie los actualiza cuando el tiempo pasa. Medido el 2026-09-17: 8 contratos
decían `vigente` con la fecha ya vencida, uno desde junio de 2025. Esa mitad la
calcula ahora `apps.contratos.services.vigencia`, que combina esta columna con
la fecha y no se puede desactualizar porque no se guarda.

Lo que queda son los tres estados que alguien elige en el wizard: el contrato
está `firmado`, se está renegociando (`en_renovacion`), o alguien lo cerró
(`terminado`).

**La conversión de las filas va dentro del `ALTER ... USING`, no en un paso
aparte.** Es una sola sentencia DDL: o se aplica entera o no se aplica. No es
la "migración de datos" que el CLAUDE.md prohíbe --esa es la que recorre filas
y puede fallar a la mitad-- sino la conversión de tipo de la propia columna.

`vencido` mapea a `firmado`, no a `terminado`: que un contrato haya pasado su
fecha no significa que alguien lo haya cerrado. La fecha lo marcará vencido al
calcular la vigencia, que es el punto de todo esto. Hoy hay 0 filas con ese
valor, así que la rama no llega a usarse.

Verificado antes de escribirla, contra producción: la columna no tiene DEFAULT
(no hay que quitarlo y reponerlo), no tiene índices ni constraints, y
`estado_contrato_enum` lo usa UNA sola columna en toda la base. Mismo patrón que
la revisión 125 de Alembic, que ya hizo esto para quitar `operacion`.

Django declara la columna como `CharField`, así que no sabe que en Postgres es
un enum: cambiar `choices` en el modelo no genera DDL. Por eso el tipo se
cambia con `RunSQL` explícito.

**Solo si el enum existe.** En una base creada desde cero (desarrollo local, las
pruebas) Django crea la columna como `varchar`: el enum es herencia de
SQLAlchemy y solo está en producción. Ahí el SQL no tiene nada que convertir y
el `DO` no hace nada; los valores los valida `choices`. Sin esa guarda,
`migrate` sobre una base vacía falla con `type "estado_contrato_enum" does not
exist`.
"""

from django.db import migrations, models

SI_HAY_ENUM = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'estado_contrato_enum') THEN
        RETURN;
    END IF;
%s
END $$;
"""

CONVERTIR = """
    CREATE TYPE estado_contrato_enum_v2 AS ENUM
        ('firmado', 'en_renovacion', 'terminado');
    ALTER TABLE contratos_servicio
        ALTER COLUMN estado TYPE estado_contrato_enum_v2
        USING (CASE estado::text
                   WHEN 'vigente' THEN 'firmado'
                   WHEN 'vencido' THEN 'firmado'
                   ELSE estado::text
               END)::estado_contrato_enum_v2;
    DROP TYPE estado_contrato_enum;
    ALTER TYPE estado_contrato_enum_v2 RENAME TO estado_contrato_enum;"""

REVERTIR = """
    CREATE TYPE estado_contrato_enum_v1 AS ENUM
        ('vigente', 'vencido', 'terminado', 'en_renovacion');
    ALTER TABLE contratos_servicio
        ALTER COLUMN estado TYPE estado_contrato_enum_v1
        USING (CASE estado::text
                   WHEN 'firmado' THEN 'vigente'
                   ELSE estado::text
               END)::estado_contrato_enum_v1;
    DROP TYPE estado_contrato_enum;
    ALTER TYPE estado_contrato_enum_v1 RENAME TO estado_contrato_enum;"""


class Migration(migrations.Migration):

    dependencies = [("contratos", "0005_alertaaniversario")]

    operations = [
        migrations.RunSQL(
            sql=SI_HAY_ENUM % CONVERTIR,
            reverse_sql=SI_HAY_ENUM % REVERTIR,
        ),
        # Solo validación de Python: `choices` no toca el esquema. Va igual para
        # que el modelo y la base digan lo mismo y el admin ofrezca lo correcto.
        migrations.AlterField(
            model_name="contratoservicio",
            name="estado",
            field=models.CharField(
                max_length=13,
                choices=[
                    ("firmado", "firmado"),
                    ("en_renovacion", "en_renovacion"),
                    ("terminado", "terminado"),
                ],
                default="firmado",
            ),
        ),
    ]
