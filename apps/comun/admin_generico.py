"""Registra en /admin/ todos los modelos de una app con un ModelAdmin armado solo.

Son ~120 modelos: escribir un ModelAdmin a mano por cada uno es ruido. Si un
modelo necesita algo propio, se registra aparte y se pasa en `excluir`.
"""

from django.apps import apps
from django.contrib import admin
from django.db import models

_NO_LISTABLES = (models.TextField, models.JSONField, models.BinaryField)


def admin_para(modelo) -> type[admin.ModelAdmin]:
    campos = [f for f in modelo._meta.concrete_fields]
    fks = [f.name for f in campos if f.is_relation]
    return type(
        f"{modelo.__name__}Admin",
        (admin.ModelAdmin,),
        {
            # Las FK fuera de la lista: cada una seria una consulta por fila.
            "list_display": [
                f.name for f in campos
                if not f.is_relation and not isinstance(f, _NO_LISTABLES)
            ][:8] or ["__str__"],
            "search_fields": [
                f.name for f in campos
                if isinstance(f, models.CharField) and not f.choices
            ][:4],
            "list_filter": [
                f.name for f in campos
                if isinstance(f, models.BooleanField) or (f.choices and not f.is_relation)
            ][:4],
            # Un <select> con miles de fallas o proyectos no carga: id a mano.
            "raw_id_fields": fks,
        },
    )


def registrar_todos(app_label: str, excluir=()) -> None:
    for modelo in apps.get_app_config(app_label).get_models():
        # El admin de Django no soporta PK compuesta (tablas puente, sobre todo).
        if modelo._meta.is_composite_pk:
            continue
        if modelo not in excluir and not admin.site.is_registered(modelo):
            admin.site.register(modelo, admin_para(modelo))
