"""La participación deja de guardar el cliente: queda solo el inversionista. IRREVERSIBLE.

    proyecto_inversionistas.cliente_id         -> se borra (con su llave a `clientes`)
    proyecto_inversionistas.inversionista_id   -> obligatorio
    UNIQUE (proyecto_id, inversionista_id)     -> un inversionista, una vez por planta

Diagrama de Sara (2026-10-13). El cliente se alcanza por `inversionista.cliente`.

Va en un push APARTE del paso 1 (`0009_inversionistas`), y después de correr en el
servidor `poblar_inversionistas --aplicar` (en `comando.yml`): ese comando llena
`inversionista_id` en las participaciones que ya existían.

**La guarda no es adorno.** Si queda una participación sin inversionista --el
comando no se corrió, o alguien registró una sin pasar por `save()`--, la
migración FALLA antes de borrar `cliente_id`, que sería el único sitio donde está
ese dato. Si falla: correr `poblar_inversionistas --aplicar` y volver a desplegar.

Respaldo opcional antes de desplegar (116 filas al 2026-10-01):

    \\copy (SELECT id, proyecto_id, cliente_id, inversionista_id FROM proyecto_inversionistas)
        TO 'proyecto_inversionistas_cliente.csv' CSV HEADER
"""
from django.db import migrations, models
import django.db.models.deletion

COMPROBAR = """
DO $$
DECLARE
    sin_vincular integer;
    distinto integer;
BEGIN
    SELECT count(*) INTO sin_vincular FROM proyecto_inversionistas WHERE inversionista_id IS NULL;
    IF sin_vincular > 0 THEN
        RAISE EXCEPTION '% participaciones sin inversionista: corre poblar_inversionistas --aplicar. '
            'No se borra nada.', sin_vincular;
    END IF;
    SELECT count(*) INTO distinto
      FROM proyecto_inversionistas pi JOIN inversionistas i ON i.id = pi.inversionista_id
     WHERE i.cliente_id IS DISTINCT FROM pi.cliente_id;
    IF distinto > 0 THEN
        RAISE EXCEPTION '% participaciones con un inversionista de otro cliente. '
            'No se borra nada.', distinto;
    END IF;
END
$$;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('proyectos', '0009_inversionistas'),
    ]

    operations = [
        # Sin vuelta atrás: no hay reverse_sql para la guarda ni para la columna.
        migrations.RunSQL(COMPROBAR),
        migrations.RemoveField(
            model_name='proyectoinversionista',
            name='cliente',
        ),
        migrations.AlterField(
            model_name='proyectoinversionista',
            name='inversionista',
            field=models.ForeignKey(db_column='inversionista_id', on_delete=django.db.models.deletion.PROTECT, related_name='participaciones', to='proyectos.inversionista'),
        ),
        migrations.AddConstraint(
            model_name='proyectoinversionista',
            constraint=models.UniqueConstraint(fields=('proyecto', 'inversionista'), name='uq_proyecto_inversionistas'),
        ),
    ]
