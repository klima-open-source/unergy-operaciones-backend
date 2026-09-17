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
"""

from django.db import migrations, models

CREAR_NUEVO = """
DO $$ BEGIN
    CREATE TYPE estado_contrato_enum_v2 AS ENUM
        ('firmado', 'en_renovacion', 'terminado');
EXCEPTION WHEN duplicate_object THEN null;
END $$;
"""

CONVERTIR = """
ALTER TABLE contratos_servicio
    ALTER COLUMN estado TYPE estado_contrato_enum_v2
    USING (CASE estado::text
               WHEN 'vigente' THEN 'firmado'
               WHEN 'vencido' THEN 'firmado'
               ELSE estado::text
           END)::estado_contrato_enum_v2;
DROP TYPE estado_contrato_enum;
ALTER TYPE estado_contrato_enum_v2 RENAME TO estado_contrato_enum;
"""

CREAR_VIEJO = """
DO $$ BEGIN
    CREATE TYPE estado_contrato_enum_v1 AS ENUM
        ('vigente', 'vencido', 'terminado', 'en_renovacion');
EXCEPTION WHEN duplicate_object THEN null;
END $$;
"""

REVERTIR = """
ALTER TABLE contratos_servicio
    ALTER COLUMN estado TYPE estado_contrato_enum_v1
    USING (CASE estado::text
               WHEN 'firmado' THEN 'vigente'
               ELSE estado::text
           END)::estado_contrato_enum_v1;
DROP TYPE estado_contrato_enum;
ALTER TYPE estado_contrato_enum_v1 RENAME TO estado_contrato_enum;
"""


class Migration(migrations.Migration):

    dependencies = [("contratos", "0005_alertaaniversario")]

    operations = [
        migrations.RunSQL(
            sql=CREAR_NUEVO + CONVERTIR,
            reverse_sql=CREAR_VIEJO + REVERTIR,
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
