"""Deploy 3 del plan `docs/refactor/08-plan-django-contratos.md`: limpieza. IRREVERSIBLE.

Va al menos dos semanas después del corte (deploy 2), con la tabla única estable, y
SOLO después del respaldo, en el servidor:

    docker compose exec <servicio de postgres o host con acceso> \
        pg_dump -t ppa_contratos -t contratos_servicio -t ppa_contrato_proyectos \
                -t contratos_servicio_correspondencia -t contratos_corte_fk_respaldo \
                <base> > contratos_viejos_<fecha>.sql

El archivo se guarda FUERA de la base. Es el único paso del plan sin vuelta atrás por
deploy: el rollback es restaurar ese dump.

Borra las tres tablas viejas (ya sin modelo desde el corte), el respaldo de llaves que
dejó `0009_corte_bd` y la tabla de correspondencia de ids. Antes de borrar comprueba que
ninguna llave foránea apunte todavía a las tablas viejas: si apareció alguna después
del corte, se detiene sin borrar nada.
"""
from django.db import migrations

BORRAR = """
DO $$
DECLARE
    quedan integer;
BEGIN
    SELECT count(*) INTO quedan
      FROM pg_constraint
     WHERE contype = 'f'
       AND confrelid IN ('ppa_contratos'::regclass, 'contratos_servicio'::regclass)
       AND conrelid <> 'ppa_contrato_proyectos'::regclass;
    IF quedan > 0 THEN
        RAISE EXCEPTION 'Todavía hay % llaves foráneas apuntando a las tablas viejas: '
            'no se borra nada.', quedan;
    END IF;
END
$$;
DROP TABLE ppa_contrato_proyectos;
DROP TABLE ppa_contratos;
DROP TABLE contratos_servicio;
DROP TABLE contratos_corte_fk_respaldo;
"""


class Migration(migrations.Migration):

    dependencies = [
        # Después de borrar las columnas de las partes: van en el mismo despliegue de limpieza.
        ("contratos", "0014_borrar_columnas_partes"),
        ("ppa", "0003_corte_estado"),
    ]

    operations = [
        # Sin reverse_sql: no hay vuelta atrás por migración, solo por el dump.
        migrations.RunSQL(BORRAR),
        migrations.DeleteModel(name="ContratoServicioCorrespondencia"),
    ]
