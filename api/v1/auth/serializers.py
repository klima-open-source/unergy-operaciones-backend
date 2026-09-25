"""Serializers de autenticación y usuarios."""

from rest_framework import serializers

from apps.plataforma import models as pl_models


class TokenSerializer(serializers.Serializer):
    access_token = serializers.CharField()
    token_type = serializers.CharField(default="bearer")


class UsuarioSerializer(serializers.ModelSerializer):
    """NUNCA expone `password` ni los campos del token de reset."""

    class Meta:
        model = pl_models.Usuario
        fields = [
            "id", "email", "nombre", "rol", "activo", "ultimo_acceso",
            "created_at", "updated_at",
        ]


class UsuarioCrearSerializer(serializers.Serializer):
    email = serializers.EmailField()
    nombre = serializers.CharField()
    rol = serializers.ChoiceField(choices=pl_models.Rol.choices)
    activo = serializers.BooleanField(default=True)
    password = serializers.CharField(write_only=True, min_length=8)


class UsuarioActualizarSerializer(serializers.Serializer):
    nombre = serializers.CharField(required=False)
    rol = serializers.ChoiceField(choices=pl_models.Rol.choices, required=False)
    activo = serializers.BooleanField(required=False)
    password = serializers.CharField(
        write_only=True, required=False, min_length=8
    )


class ForgotPasswordSerializer(serializers.Serializer):
    email = serializers.CharField()


class ResetPasswordSerializer(serializers.Serializer):
    token = serializers.CharField()
    new_password = serializers.CharField()
