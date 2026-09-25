from django.contrib import admin
from django.contrib.auth.models import Group

from apps.comun.admin_generico import registrar_todos
from apps.plataforma.models import Usuario

# Sin `PermissionsMixin` los grupos no aplican: los permisos salen de `rol`.
admin.site.unregister(Group)


@admin.register(Usuario)
class UsuarioAdmin(admin.ModelAdmin):
    # Nunca `password` ni el token de reset: la contrasena se cambia por
    # el API (o `manage.py changepassword`), no editando el hash.
    fields = ["email", "nombre", "rol", "activo", "ultimo_acceso", "created_at", "updated_at"]
    readonly_fields = ["ultimo_acceso", "created_at", "updated_at"]
    list_display = ["email", "nombre", "rol", "activo", "ultimo_acceso"]
    list_filter = ["rol", "activo"]
    search_fields = ["email", "nombre"]


registrar_todos("plataforma", excluir={Usuario})
