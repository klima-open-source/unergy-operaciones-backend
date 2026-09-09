"""Settings del proyecto Django.

Lee las MISMAS variables de entorno que la app FastAPI (`app/core/config.py`),
a proposito: mientras los dos backends coexisten apuntan a la base `operations`
con la misma configuracion, y no hay un segundo .env que mantener sincronizado.

Dos decisiones que definen toda la migracion:

1. **Alembic sigue siendo el dueno del esquema.** Todos los modelos declaran
   `managed = False`, asi que `makemigrations` no genera DDL y Django nunca
   toca la estructura de la base. Mientras FastAPI siga leyendo estas tablas
   solo puede haber UN dueno del esquema, y ya es Alembic. Ver `apps/README.md`
   para cuando se invierte esto.

2. **`APPEND_SLASH = False`.** FastAPI sirve `/api/v1/retos` sin barra final.
   Django redirige a `/retos/` por defecto, lo que convertiria cada POST del
   frontend en un 301 que pierde el cuerpo. Va junto con
   `DefaultRouter(trailing_slash=False)` en cada `urls.py` de recurso.
"""

import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# FastAPI lee el .env solo: pydantic-settings lo declara con `env_file=".env"`.
# Django no hace nada parecido -- `os.getenv` mira el proceso y ya --, asi que
# sin esta linea `manage.py` fuera del contenedor arrancaba con TODOS los
# defaults: SECRET_KEY vacio y la base en localhost/postgres/postgres, sin un
# solo aviso. Dentro del contenedor no se notaba porque el compose inyecta el
# .env con `env_file:`, y por eso podia pasar mucho tiempo sin verse.
# `override=False`: lo que ya venga en el entorno MANDA sobre el archivo, que es
# como se comporta pydantic y lo que permite `SECRET_KEY=... pytest`.
load_dotenv(BASE_DIR / ".env", override=False)

ENTORNO = os.getenv("ENVIRONMENT", "development").lower()

SECRET_KEY = os.getenv("SECRET_KEY", "")
# La variable se llama ENVIRONMENT, no ENV -- asi la define el .env y asi la lee
# `app/core/config.py`. Con el nombre equivocado esto daba DEBUG=True SIEMPRE,
# tambien en produccion: cualquier 500 habria devuelto el traceback con las
# variables locales (credenciales incluidas) y ALLOWED_HOSTS no se aplicaria.
DEBUG = ENTORNO != "production"
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "*").split(",")

# El `Client` de django.test manda `Host: testserver`, asi que un ALLOWED_HOSTS
# con el dominio real —lo correcto en produccion— hace que TODA peticion de una
# prueba devuelva 400 DisallowedHost. Se agrega solo bajo pytest: en el
# contenedor `pytest` no esta importado y la lista queda tal cual la define el
# .env. Aqui y no en cada fixture para que valga tambien para la proxima prueba
# que use el Client.
if "pytest" in sys.modules and "testserver" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, "testserver"]

# Los JWT se firman con SECRET_KEY. Vacia, jose firma con "" y cualquiera puede
# forjar un token valido para cualquier sub/rol: toma total de una cuenta admin.
# Mismo criterio que el validador de `app/core/config.py` -- en produccion falla
# el arranque, en desarrollo solo advierte, para no estorbar.
if not SECRET_KEY and ENTORNO != "development":
    raise RuntimeError(
        "[SEGURIDAD] SECRET_KEY no esta configurado en produccion. Define la "
        "variable de entorno con una clave aleatoria de 32+ caracteres."
    )

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "django_celery_beat",
    # Dominios (arbol A). Una linea por app portada, nucleo primero.
    "apps.plataforma",
    "apps.proyectos",
    "apps.clientes",
    "apps.fronteras",
    "apps.contratos",
    "apps.comercial",
    "apps.ppa",
    "apps.facturacion",
    "apps.mercado_xm",
    "apps.liquidaciones",
    "apps.registros_cnd",
    "apps.energia",
    "apps.monitoreo",
    "apps.om",
    "apps.arriendos",
    "apps.contabilidad",
    "apps.mandatos",
    "apps.garantias",
    "apps.retos",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# Las piezas POSTGRES_*/PG_* son la forma preferida (se leen mejor en el .env y
# rotar la clave no obliga a rearmar una URL). Pero si DATABASE_URL esta
# definida GANA, igual que en `app/core/config.py`: la entregan los proveedores
# gestionados de un solo pegue y los .env viejos la traen. Sin esta rama, un
# entorno con DATABASE_URL dejaba a los dos backends apuntando a bases
# DISTINTAS -- FastAPI a la URL, Django a las piezas (o a sus defaults).
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "operaciones"),
        "USER": os.getenv("POSTGRES_USER", "postgres"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "postgres"),
        "HOST": os.getenv("PG_HOST", "localhost"),
        "PORT": os.getenv("PG_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}

if os.getenv("DATABASE_URL"):
    # A mano con urlparse y no con dj-database-url: son seis lineas contra una
    # dependencia mas, y el unico esquema que hay que entender es el de esta
    # casa. `+psycopg` es del dialecto de SQLAlchemy y no significa nada para
    # Django, asi que se ignora: lo que importa son las cinco piezas.
    url = urlparse(os.environ["DATABASE_URL"])
    DATABASES["default"].update({
        "NAME": url.path.lstrip("/") or DATABASES["default"]["NAME"],
        "USER": unquote(url.username or ""),
        "PASSWORD": unquote(url.password or ""),
        "HOST": url.hostname or "",
        "PORT": str(url.port or ""),
    })

# Bases EXTERNAS de otros servicios, solo lectura y por psycopg crudo (ver
# apps/proyectos/services/mapa_externo.py). No van en DATABASES a propósito: no
# se les aplica ninguna migración y su esquema no es nuestro.
REQUESTSDB_DATABASE_URL = os.getenv("REQUESTSDB_DATABASE_URL", "")
ORIGINA_DATABASE_URL = os.getenv("ORIGINA_DATABASE_URL", "")

REST_FRAMEWORK = {
    # El ORDEN importa: la API key se mira antes que el Bearer, igual que en
    # FastAPI. Ver el docstring de api/authentication.py.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "api.authentication.ApiKeyAuthentication",
        "api.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "api.pagination.BasePagination",
    "PAGE_SIZE": 50,
    "UNAUTHENTICATED_USER": None,
    # Repone el cuerpo dict bajo `detail`: DRF lo devuelve crudo en la raiz
    # y el frontend lee `e.data.detail`. Ver api/exceptions.py.
    "EXCEPTION_HANDLER": "api.exceptions.manejador_de_excepciones",
    # Global a proposito: el default de DRF es True y serializa cada `Decimal`
    # como string ("2205.225"), donde Pydantic mandaba el numero. Cualquier
    # aritmetica del frontend sobre esos campos daba NaN.
    "COERCE_DECIMAL_TO_STRING": False,
}

# El contenedor corre en UTC y el codigo asume UTC (ver _hoy_col()).
TIME_ZONE = "UTC"
USE_TZ = True
LANGUAGE_CODE = "es-co"

APPEND_SLASH = False                       # ver docstring del modulo

CORS_ALLOW_ALL_ORIGINS = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# Cache compartida entre procesos -- el MISMO Redis que ya usa Celery. No esta
# aca para acelerar consultas: es donde vive el estado que dos procesos
# distintos tienen que VER (el resultado de la ultima corrida del reporte de
# energia y la bandera de "Detener", ver orquestador.py). Sin esto Django usa
# LocMemCache, que es memoria por proceso: con `--workers 3` cada gunicorn
# tendria su propia copia y el clic de "Detener" caeria en el worker
# equivocado, respondiendo OK sin detener nada.
#
# Sin dependencia nueva: `celery[redis]` ya trae `redis` y el backend viene con
# Django.
#
# **Base 1, NUNCA la 0.** El broker de Celery vive en la 0 y un `cache.clear()`
# hace FLUSHDB: se llevaria la cola de tareas por delante. La 1 se deriva de la
# URL del broker para no tener que configurar dos variables que siempre apuntan
# al mismo Redis; `REDIS_CACHE_URL` la pisa si algun dia hace falta separarlos.
#
# **Lo que se guarde aca no es durable.** El Redis del compose corre con
# `--save "" --appendonly no`, o sea sin persistencia: un reinicio lo vacia.
# Sirve para estado efimero (que es lo que era antes: memoria de un proceso).
# Nada que haya que poder leer con certeza manana va aca -- eso es una tabla.
_cache_url = os.getenv("REDIS_CACHE_URL")
if not _cache_url:
    _base, _, _db = CELERY_BROKER_URL.rpartition("/")
    _cache_url = f"{_base}/1" if _db.isdigit() else f"{CELERY_BROKER_URL.rstrip('/')}/1"

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": _cache_url,
        "KEY_PREFIX": "operaciones",
    }
}

# Los cron de las tareas estan en hora de Bogota, no en la del contenedor (UTC),
# igual que en el BackgroundScheduler de FastAPI. Sin esto la clasificacion de
# las 3:30am correria a las 10:30pm del dia anterior.
CELERY_TIMEZONE = os.getenv("TIMEZONE", "America/Bogota")
CELERY_ENABLE_UTC = False

from config.horarios import HORARIOS  # noqa: E402

CELERY_BEAT_SCHEDULE = HORARIOS
