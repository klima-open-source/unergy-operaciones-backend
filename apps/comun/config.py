"""Configuración del entorno con acceso por atributo.

Los servicios portados desde FastAPI usan `settings.UNERGY_API_URL` y no
`os.environ["UNERGY_API_URL"]`. Este objeto conserva esa forma para que el
código venga tal cual del original: cuanto menos se le toque a un cliente de una
API externa, menos hay que volver a verificar contra esa API.

No es `django.conf.settings` a propósito: son credenciales de terceros que solo
usan dos o tres módulos, y meterlas en `config/settings.py` obliga a declarar
cada variable nueva en dos sitios.

**Los defaults de abajo NO son decoración.** `app/core/config.py` los declaraba
como valores por defecto de pydantic, así que el `.env` nunca necesitó traerlos
y de hecho no los trae. Un shim que devolviera `""` para todo dejaba, por
ejemplo, `UNERGY_API_URL` vacío en producción: las diez llamadas que arman su
URL con ese prefijo pedirían contra una ruta relativa y fallarían sin que el
`.env` tuviera nada raro a la vista. Cada entrada acá es un default que existía
en FastAPI y que este objeto tiene que seguir dando.

`apps/liquidaciones/services/api_externa.py` y
`apps/energia/services/unergy_api.py` tienen su propia copia de esto, anterior a
este módulo; deberían converger acá.
"""

import os

# Los valores por defecto que traía `Settings` de pydantic. Solo los que NO son
# la cadena vacía: para el resto, ausente y vacío significan lo mismo.
DEFECTOS = {
    "UNERGY_API_URL": "https://api.unergy.io",
    # El default tiene que ser PRODUCCION, no localhost: este valor termina en el
    # boton "Ver detalle de la falla" del correo que se le manda AL CLIENTE
    # (app/services/email_service.py:483). Con `http://localhost:5173` --el
    # default anterior, heredado de FastAPI-- el cliente recibia un enlace que no
    # lleva a ninguna parte, y del lado de la plataforma no se nota: el correo
    # sale bien y el boton se ve bien. Encima el puerto tambien estaba viejo, de
    # antes de Nuxt (`nuxt dev` usa 3000, no 5173).
    #
    # Para desarrollo local, el `.env.example` trae el localhost correcto.
    "FRONTEND_URL": "https://operaciones.unergy.io",
    "COMERCIAL_ALERTA_DIAS": "5",
    "IMAP_HOST": "imap.gmail.com",
    "IMAP_PORT": "993",
    "SMTP_PORT": "587",
    "SMTP_FROM": "operaciones@unergy.io",
    "TIMEZONE": "America/Bogota",
    "PPA_ALERT_DAYS": "90,60,30",
    "PPA_ALERT_EMAILS": "adhara@unergy.io,jessica@unergy.io",
    # Solenium migro de solenium.co a sole.tech. El dominio viejo esta MUERTO:
    # resuelve por DNS pero el TLS lo rechaza (TLSV1_UNRECOGNIZED_NAME), asi que
    # no falla como un 404 legible sino como un error de conexion. Verificado el
    # 2026-09-07 contra los dos dominios; las rutas (/api/token/, /api/) no
    # cambiaron. Estos defaults NO estan en el .env (ver el docstring de
    # apps/comun/config.py), asi que este archivo es el que manda.
    "SUNFACTORY_API_URL": "https://sunfactory.sole.tech/api",
    "SUNFACTORY_AUTH_URL": "https://auth.sole.tech/api/token/",
    "SOLENIUM_AUTH_URL": "https://auth.sole.tech/api",
    "SOLENIUM_DATA_URL": "https://data.sole.tech/api",
    "SOLARVIEW_BASE_URL": "https://api.sole.tech",
    "QUOIA_BASE_URL": "https://gaia.quoia.energy/api",
    "GAIA_BASE_URL": "https://gaia.quoia.energy",
    "STORAGE_BACKEND": "local",
    "STORAGE_LOCAL_PATH": "./uploads",
    "APP_NAME": "Plataforma Operaciones Unergy",
}


class _Entorno:
    def __getattr__(self, nombre: str) -> str:
        # El entorno manda; el default solo cubre la ausencia. Una variable
        # puesta a propósito en vacío sigue siendo vacío.
        valor = os.environ.get(nombre)
        return valor if valor is not None else DEFECTOS.get(nombre, "")


settings = _Entorno()
