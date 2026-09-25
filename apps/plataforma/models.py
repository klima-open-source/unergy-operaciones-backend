"""Usuarios, roles y auditoria — el nucleo del que dependen las demas apps.

14 modulos importan este modelo hoy; es, junto con `proyectos`, lo primero que
tiene que existir en Django para que cualquier otra rebanada se pueda portar.

Django posee el esquema de estas tablas desde el 2026-09-04. Los modelos son
`managed` (el default): `makemigrations` genera DDL real y `migrate` lo aplica.
Alembic quedo congelado en la revision 143 -- ver apps/README.md.

Este archivo se mantiene A MANO: `Timer` y `Rol` no salen de ningun metadato y
el generador los borraria. Por eso no lleva la marca "GENERADO por" — es lo que
hace que `scripts/generar_modelos_django.py` se niegue a pisarlo.
"""

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.utils import timezone


class Timer(models.Model):
    """Base abstracta de timestamps.

    Se declara aca y cada app la importa, en vez de repetirla por app como hace
    Origina: son las mismas dos columnas en las 115 tablas y no hay razon para
    tener 18 copias.
    """

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Fecha de creación")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Fecha de actualización")

    class Meta:
        abstract = True


class Rol(models.TextChoices):
    ADMIN = "admin", "Administrador"
    OPERACIONES = "operaciones", "Operaciones"
    MONITOREO = "monitoreo", "Monitoreo"
    LIQUIDACIONES = "liquidaciones", "Liquidaciones"
    CGM = "cgm", "CGM"
    SOLO_LECTURA = "solo_lectura", "Solo lectura"
    COORDINADOR = "coordinador", "Coordinador"
    TECNICO = "tecnico", "Técnico"
    COMERCIAL = "comercial", "Comercial"


class UsuarioManager(BaseUserManager):
    def create_user(self, email, nombre, rol=Rol.SOLO_LECTURA, password=None, **extra):
        usuario = self.model(email=self.normalize_email(email), nombre=nombre, rol=rol, **extra)
        usuario.set_password(password)
        usuario.save(using=self._db)
        return usuario

    def create_superuser(self, email, nombre, rol=Rol.ADMIN, password=None, **extra):
        return self.create_user(email, nombre, Rol.ADMIN, password, **extra)


class Usuario(AbstractBaseUser):
    """El `AUTH_USER_MODEL`. Se monta sobre la tabla `usuarios` TAL CUAL existe.

    El campo `password` es la columna `password_hash` (se llama asi porque
    `createsuperuser` y `changepassword` lo buscan por nombre), con el bcrypt
    crudo de `seguridad` -- el mismo del login del API, no el formato de los
    hashers de Django. `last_login` se anula: esa columna no existe (el API usa
    `ultimo_acceso`). Tampoco hay `PermissionsMixin`: los permisos salen de
    `rol`, y un `admin` activo lo puede todo en /admin/.
    """

    last_login = None

    id = models.BigAutoField(primary_key=True)
    email = models.CharField(max_length=255, unique=True, verbose_name="Correo")
    nombre = models.CharField(max_length=255, verbose_name="Nombre")
    # La columna es el enum `rol_enum` de PostgreSQL. Django la lee como texto;
    # las opciones se validan aca y el tipo sigue existiendo en la base.
    rol = models.CharField(max_length=20, choices=Rol.choices, verbose_name="Rol")
    activo = models.BooleanField(default=True, verbose_name="Activo")
    password = models.CharField(max_length=255, null=True, blank=True, db_column="password_hash")
    ultimo_acceso = models.DateTimeField(null=True, blank=True, verbose_name="Último acceso")
    password_reset_token = models.CharField(max_length=255, null=True, blank=True)
    password_reset_expires = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UsuarioManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["nombre"]

    class Meta:
        db_table = "usuarios"
        verbose_name = "Usuario"
        verbose_name_plural = "Usuarios"

    def __str__(self) -> str:
        return self.email

    @property
    def is_active(self) -> bool:
        return self.activo

    @property
    def is_staff(self) -> bool:
        return self.activo and self.rol == Rol.ADMIN

    is_superuser = is_staff

    def has_perm(self, perm, obj=None) -> bool:
        return self.is_staff

    def has_module_perms(self, app_label) -> bool:
        return self.is_staff

    def set_password(self, raw_password):
        from apps.plataforma.services import seguridad

        self.password = seguridad.hash_contrasena(raw_password) if raw_password else None

    def check_password(self, raw_password) -> bool:
        from apps.plataforma.services import seguridad

        return bool(self.password) and seguridad.verificar_contrasena(raw_password, self.password)

    def set_unusable_password(self):
        self.password = None

    def has_usable_password(self) -> bool:
        return bool(self.password)

    def get_session_auth_hash(self):
        # La sesion del admin muere si cambia la contrasena, igual que en Django.
        from django.utils.crypto import salted_hmac

        return salted_hmac("apps.plataforma.Usuario", self.password or "", algorithm="sha256").hexdigest()


class Notificacion(models.Model):
    id = models.BigAutoField(primary_key=True)
    usuario = models.ForeignKey("Usuario", on_delete=models.CASCADE, db_column="usuario_id", related_name="notificaciones")
    tipo = models.CharField(max_length=6, choices=[("alerta", "alerta"), ("info", "info"), ("accion", "accion")])
    titulo = models.CharField(max_length=500)
    mensaje = models.TextField()
    leida = models.BooleanField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "notificaciones"


class InformeGuardado(models.Model):
    id = models.BigAutoField(primary_key=True)
    tipo = models.CharField(max_length=20, choices=[("op", "op"), ("fmo", "fmo"), ("port", "port"), ("ranking", "ranking"), ("pm", "pm")])  # la columna es varchar(20)
    sub_project = models.CharField(max_length=200)
    periodo_desde = models.CharField(max_length=10)
    periodo_hasta = models.CharField(max_length=10)
    periodo_display = models.CharField(max_length=100, null=True, blank=True)
    proyecto_nombre = models.CharField(max_length=300, null=True, blank=True)
    html_content = models.TextField()
    charts_data = models.JSONField(null=True, blank=True)
    estado = models.CharField(max_length=20, choices=[("borrador", "borrador"), ("revisado", "revisado"), ("aprobado", "aprobado")])  # la columna es varchar(20)
    creado_por = models.ForeignKey("Usuario", on_delete=models.SET_NULL, db_column="creado_por_id", null=True, blank=True, related_name="informes_guardados_por_creado_por_id")
    editado_por = models.ForeignKey("Usuario", on_delete=models.SET_NULL, db_column="editado_por_id", null=True, blank=True, related_name="informes_guardados_por_editado_por_id")
    aprobado_por = models.ForeignKey("Usuario", on_delete=models.SET_NULL, db_column="aprobado_por_id", null=True, blank=True, related_name="informes_guardados_por_aprobado_por_id")
    creado_por_nombre = models.CharField(max_length=255, null=True, blank=True)
    editado_por_nombre = models.CharField(max_length=255, null=True, blank=True)
    aprobado_por_nombre = models.CharField(max_length=255, null=True, blank=True)
    creado_en = models.DateTimeField(default=timezone.now)
    editado_en = models.DateTimeField(null=True, blank=True)
    aprobado_en = models.DateTimeField(null=True, blank=True)
    correo_enviado = models.BooleanField(default=False)
    correo_enviado_en = models.DateTimeField(null=True, blank=True)
    comentarios = models.JSONField(null=True, blank=True)
    miembros = models.JSONField(null=True, blank=True)
    enviado_por = models.ForeignKey("Usuario", on_delete=models.SET_NULL, db_column="enviado_por_id", null=True, blank=True, related_name="informes_guardados_por_enviado_por_id")
    enviado_por_nombre = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = "informes_guardados"


class ApiKey(models.Model):
    """Clave de API por usuario, para integraciones sin login interactivo.

    **Esta tabla NO tiene modelo SQLAlchemy.** Se creó desde `_PENDING_DDLS` y
    quedó capturada en la revisión 135 de Alembic; el router de FastAPI la
    consulta con SQL crudo (`text("SELECT * FROM api_keys …")`). Declararla acá
    es lo que la vuelve visible para el ORM. Las columnas se transcribieron del
    DDL de esa revisión, que es la fuente de verdad.

    Quedan 9 tablas más en la misma situación; ver `apps/README.md`.
    """

    id = models.BigAutoField(primary_key=True)
    usuario = models.ForeignKey(
        Usuario, on_delete=models.CASCADE, db_column="usuario_id",
        related_name="api_keys", db_index=True, verbose_name="Usuario",
    )
    nombre = models.CharField(max_length=255, verbose_name="Nombre")
    # Solo el hash SHA-256. La clave en claro se devuelve UNA vez, al crearla, y
    # no se puede recuperar después: es lo que hace que una filtración de la
    # tabla no entregue claves usables.
    key_hash = models.CharField(max_length=255, unique=True)
    key_prefix = models.CharField(max_length=12, db_index=True, verbose_name="Prefijo")
    scopes = models.JSONField(default=list, verbose_name="Alcances")
    activo = models.BooleanField(default=True, verbose_name="Activa")
    ultimo_uso = models.DateTimeField(null=True, blank=True, verbose_name="Último uso")
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name="Expira")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "api_keys"
        verbose_name = "API Key"
        verbose_name_plural = "API Keys"

    def __str__(self) -> str:
        return f"{self.nombre} ({self.key_prefix}…)"
