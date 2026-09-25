from django.db import migrations, models


class Migration(migrations.Migration):
    """`password_hash` -> `password` en Python; la columna NO cambia.

    Solo estado: con `RenameField` a secas Django renombra la columna a
    `password` y el `AlterField` la devuelve -- dos ALTER sobre `usuarios`
    para quedar igual.
    """

    dependencies = [("plataforma", "0003_alter_informeguardado_correo_enviado")]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RenameField("usuario", "password_hash", "password"),
                migrations.AlterField(
                    "usuario", "password",
                    models.CharField(max_length=255, null=True, blank=True, db_column="password_hash"),
                ),
            ],
        ),
    ]
