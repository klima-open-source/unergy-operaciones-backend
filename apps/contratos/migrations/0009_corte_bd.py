"""Deploy 2 del plan `docs/refactor/08-plan-django-contratos.md`: el corte, en la BASE.

Es la ÚNICA migración del corte que ejecuta SQL; las demás (de esta app y de los
satélites) solo cambian el estado de Django. Así todo el trabajo sobre la base vive en
una sola migración, y una migración en Postgres es una transacción: o pasa entero, o
no pasa nada y el deploy se detiene con la app vieja intacta.

Qué hace, en orden:

0. Comprueba que la copia (`manage.py backfill_contratos_unificados`) está hecha y
   completa: cada contrato de servicio tiene su id nuevo y cada PPA está en
   `contratos` con su mismo id. Si no, se detiene sin tocar nada.
1. Busca TODAS las llaves foráneas que apuntan a `ppa_contratos` o
   `contratos_servicio` (menos la de `ppa_contrato_proyectos`, que es una tabla vieja
   más), las guarda en `contratos_corte_fk_respaldo` con su definición completa, y las
   quita. Se buscan en la base y no en una lista: así entra también la de `garantias`
   (tabla sin modelo, vacía en la copia local) y cualquier otra que exista en
   producción y no en la copia.
2. Reescribe los ids de servicio en esas columnas con
   `contratos_servicio_correspondencia` (viejo → nuevo). Los de PPA no cambian
   (decisión 1). Los ids nuevos están todos por encima de los viejos, así que el
   UPDATE no choca con ningún UNIQUE a mitad de camino.
3. Las vuelve a crear con el mismo nombre y el mismo ON DELETE, apuntando a
   `contratos`.

**Excepción a `CLAUDE.md`, a propósito (decisión 3 del plan, Sara 2026-10-06):** esto
mueve datos dentro de una migración. Cambiar de tabla y reescribir los ids tiene que
ser el mismo instante: antes, la app vieja no encuentra el contrato; después, la nueva
lo cruza con otro. La razón de la regla —que un fallo deje el deploy a medias— no
aplica aquí: si algo falla, Postgres deshace la migración completa.

Las tablas viejas NO se tocan: siguen con sus datos hasta el deploy 3.

**Rollback** (`migrate contratos 0008`): restaura las llaves foráneas desde el
respaldo y devuelve los ids viejos. Es fiel solo mientras no se hayan creado
contratos ni filas nuevas después del corte: la app nueva escribe solo en `contratos`,
y lo creado ahí no existe en las tablas viejas. Pasado eso, la reversa falla al volver
a crear las llaves (no deja nada a medias: es una transacción).
"""
from django.db import migrations

TABLAS_VIEJAS = "('ppa_contratos'::regclass, 'contratos_servicio'::regclass)"

ADELANTE = f"""
DO $$
DECLARE
    r record;
    faltan integer;
BEGIN
    -- 0. La copia tiene que estar hecha y completa.
    SELECT count(*) INTO faltan
      FROM contratos_servicio cs
      LEFT JOIN contratos_servicio_correspondencia m ON m.id_viejo = cs.id
     WHERE m.id_viejo IS NULL;
    IF faltan > 0 THEN
        RAISE EXCEPTION 'Hay % contratos de servicio sin copiar: corre '
            'manage.py backfill_contratos_unificados --reset antes del corte.', faltan;
    END IF;
    SELECT count(*) INTO faltan
      FROM ppa_contratos p
      LEFT JOIN contratos c ON c.id = p.id AND c.grupo = 'ppa'
     WHERE c.id IS NULL;
    IF faltan > 0 THEN
        RAISE EXCEPTION 'Hay % PPA sin copiar con su id: corre '
            'manage.py backfill_contratos_unificados --reset antes del corte.', faltan;
    END IF;

    -- 1. Respaldar y quitar las llaves que apuntan a las tablas viejas.
    CREATE TABLE contratos_corte_fk_respaldo (
        tabla text NOT NULL,
        columna text NOT NULL,
        nombre text NOT NULL,
        definicion text NOT NULL,
        apuntaba_a text NOT NULL
    );
    INSERT INTO contratos_corte_fk_respaldo
    SELECT con.conrelid::regclass::text, a.attname, con.conname,
           pg_get_constraintdef(con.oid), con.confrelid::regclass::text
      FROM pg_constraint con
      JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
     WHERE con.contype = 'f'
       AND con.confrelid IN {TABLAS_VIEJAS}
       AND con.conrelid <> 'ppa_contrato_proyectos'::regclass;

    FOR r IN SELECT * FROM contratos_corte_fk_respaldo LOOP
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', r.tabla, r.nombre);
    END LOOP;

    -- 2. Ids de servicio: viejo -> nuevo. Los de PPA no cambian.
    FOR r IN SELECT * FROM contratos_corte_fk_respaldo
              WHERE apuntaba_a = 'contratos_servicio' LOOP
        EXECUTE format(
            'UPDATE %I t SET %I = m.contrato_id '
            'FROM contratos_servicio_correspondencia m WHERE t.%I = m.id_viejo',
            r.tabla, r.columna, r.columna);
    END LOOP;

    -- 3. Las mismas llaves (nombre y ON DELETE), apuntando a `contratos`.
    FOR r IN SELECT * FROM contratos_corte_fk_respaldo LOOP
        EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I %s', r.tabla, r.nombre,
            replace(r.definicion, 'REFERENCES ' || r.apuntaba_a || '(id)',
                    'REFERENCES contratos(id)'));
    END LOOP;
END
$$;
"""

ATRAS = """
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT * FROM contratos_corte_fk_respaldo LOOP
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', r.tabla, r.nombre);
    END LOOP;
    FOR r IN SELECT * FROM contratos_corte_fk_respaldo
              WHERE apuntaba_a = 'contratos_servicio' LOOP
        EXECUTE format(
            'UPDATE %I t SET %I = m.id_viejo '
            'FROM contratos_servicio_correspondencia m WHERE t.%I = m.contrato_id',
            r.tabla, r.columna, r.columna);
    END LOOP;
    FOR r IN SELECT * FROM contratos_corte_fk_respaldo LOOP
        EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I %s',
                       r.tabla, r.nombre, r.definicion);
    END LOOP;
    DROP TABLE contratos_corte_fk_respaldo;
END
$$;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("contratos", "0008_tablas_unificadas_vacias"),
    ]

    operations = [
        migrations.RunSQL(ADELANTE, reverse_sql=ATRAS),
    ]
