from urllib.parse import quote_plus

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


    APP_NAME: str = "Plataforma Operaciones Unergy"
    ENVIRONMENT: str = "development"
    # El default tiene que ser PRODUCCION, no localhost: este valor termina en el
    # boton "Ver detalle de la falla" del correo que se le manda AL CLIENTE
    # (app/services/email_service.py:483). Con `http://localhost:5173` --el
    # default anterior, heredado de FastAPI-- el cliente recibia un enlace que no
    # lleva a ninguna parte, y del lado de la plataforma no se nota: el correo
    # sale bien y el boton se ve bien. Encima el puerto tambien estaba viejo, de
    # antes de Nuxt (`nuxt dev` usa 3000, no 5173).
    #
    # Para desarrollo local, el `.env.example` trae el localhost correcto.
    FRONTEND_URL: str = "https://operaciones.unergy.io"

    # Credenciales de la base en piezas, como el resto de los servicios de la
    # casa (originabot). Es la forma preferida: se leen mejor en el .env y rotar
    # la contraseña no obliga a rearmar una URL a mano.
    POSTGRES_DB: str = "operaciones"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    PG_HOST: str = "localhost"
    PG_PORT: int = 5432

    # Si se define, GANA sobre las piezas de arriba. Sirve para las URLs que
    # entregan los proveedores gestionados de un solo pegue (y es lo que estaba
    # antes, asi que los .env viejos siguen funcionando sin tocarlos).
    DATABASE_URL: str = ""

    # Bases de OTROS servicios de Unergy, solo lectura, para el mapa
    # (`app/api/v1/mapa.py`). Vacias = ese endpoint responde sin esos datos.
    # Estaban en el .env y en el codigo, pero no declaradas aca: `settings.
    # ORIGINA_DATABASE_URL` lanzaba AttributeError y /mapa/* devolvia 500.
    ORIGINA_DATABASE_URL: str = ""
    REQUESTSDB_DATABASE_URL: str = ""

    SECRET_KEY: str = ""
    JWT_EXPIRE_MINUTES: int = 480
    # Token de larga duración para la app móvil (PWA) — default 30 días
    MOBILE_JWT_EXPIRE_MINUTES: int = 43200
    # CRM comercial: días sin respuesta antes de alertar (configurable por env).
    COMERCIAL_ALERTA_DIAS: int = 5
    # La actualización comercial de julio 2026 se aplica UNA vez y se marca sola
    # (ver app/services/comercial_actualizacion.MARCA_VERSION). Poner esto en
    # true fuerza a reaplicarla, pisando lo que se haya cambiado a mano después.
    COMERCIAL_REAPLICAR_ACTUALIZACION: bool = False

    @field_validator("SECRET_KEY", mode="after")
    @classmethod
    def secret_key_must_be_set(cls, v: str, info) -> str:
        # Los JWT se firman con SECRET_KEY. Con una clave vacía, jose firma con ""
        # y cualquiera puede forjar un token válido para cualquier sub/rol (toma
        # total de cuenta admin). En producción esto debe FALLAR el arranque, no
        # solo advertir. En desarrollo se mantiene la advertencia para no estorbar.
        env = (info.data.get("ENVIRONMENT") or "development").lower()
        if not v:
            if env != "development":
                raise ValueError(
                    "[SEGURIDAD] SECRET_KEY no está configurado en producción. "
                    "Define la variable de entorno SECRET_KEY en Railway con una "
                    "clave aleatoria de 32+ caracteres."
                )
            import warnings
            warnings.warn(
                "[SEGURIDAD] SECRET_KEY no está configurado; usando vacío en "
                "desarrollo. NO desplegar así a producción.",
                stacklevel=2,
            )
        elif len(v) < 32:
            import warnings
            warnings.warn(
                "[SEGURIDAD] SECRET_KEY es más corto que 32 caracteres; usa una "
                "clave aleatoria más larga para firmar JWT de forma segura.",
                stacklevel=2,
            )
        return v

    STORAGE_BACKEND: str = "local"
    STORAGE_LOCAL_PATH: str = "./uploads"
    S3_BUCKET: str = ""
    S3_ENDPOINT: str = ""
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""

    # Unergy API credentials (used by _legacy bridge)
    UNERGY_API_URL: str = "https://api.unergy.io"
    UNERGY_ACCOUNT_ID: str = ""
    UNERGY_LOGIN: str = ""
    UNERGY_PASSWORD: str = ""

    # API de Liquidaciones (mismo host que UNERGY_API_URL). Requiere una cuenta
    # que pertenezca al grupo `admin` Y tenga is_staff=True: /api/liquidaciones/*
    # exige lo primero y /api/admin/* lo segundo. Si se dejan vacías se usan las
    # credenciales UNERGY_* de arriba.
    LIQUIDACIONES_LOGIN: str = ""
    LIQUIDACIONES_PASSWORD: str = ""
    # Clave de Gemini con la que la API lee los PDF de las facturas de XM para
    # sacarles mes y año. Es OPCIONAL: si se deja vacía, esa API usa la suya.
    # Va aquí y no en un campo de la pantalla porque es un secreto: puesto en el
    # navegador quedaría a la vista de cualquiera que abra la página.
    LIQUIDACIONES_GEMINI_API_KEY: str = ""

    # Sun Factory — Solenium EPC, cronogramas de construcción (próximos a energizarse).
    # Auth = auth.solenium.co/api/token/ (username/password → JWT access).
    SUNFACTORY_API_URL: str = "https://sunfactory.sole.tech/api"
    SUNFACTORY_AUTH_URL: str = "https://auth.sole.tech/api/token/"
    SUNFACTORY_USERNAME: str = ""
    SUNFACTORY_PASSWORD: str = ""

    # Solenium API (FMO inverter data) — OAuth2 username/password
    # Solenium migro de solenium.co a sole.tech. El dominio viejo esta MUERTO:
    # resuelve por DNS pero el TLS lo rechaza (TLSV1_UNRECOGNIZED_NAME), asi que
    # no falla como un 404 legible sino como un error de conexion. Verificado el
    # 2026-09-07 contra los dos dominios; las rutas (/api/token/, /api/) no
    # cambiaron. Estos defaults NO estan en el .env (ver el docstring de
    # apps/comun/config.py), asi que este archivo es el que manda.
    SOLENIUM_AUTH_URL: str = "https://auth.sole.tech/api"
    SOLENIUM_DATA_URL: str = "https://data.sole.tech/api"
    SOLENIUM_USER: str = ""
    SOLENIUM_PASS: str = ""

    # SolarView API (reemplazo de Solenium, Fase 1: solo Reporte de Energía)
    # — token estático por header, sin login/refresh.
    SOLARVIEW_BASE_URL: str = "https://api.sole.tech"
    SOLARVIEW_TOKEN: str = ""

    # Quoia CGM API (fronteras / medidores) — legacy token auth
    QUOIA_API_TOKEN: str = ""
    QUOIA_BASE_URL: str = "https://gaia.quoia.energy/api"

    # Gaia JWT auth (for /api/cgm/v1/border + /api/node measurements)
    GAIA_USER: str = ""
    GAIA_PASS: str = ""
    GAIA_BASE_URL: str = "https://gaia.quoia.energy"

    # MGS Alarms polling
    MGS_ENABLED: bool = True
    MGS_POLL_INTERVAL_MINUTES: int = 15
    TIMEZONE: str = "America/Bogota"

    # Alertas proactivas de vencimiento de contratos PPA.
    # Lista separada por comas de dias de antelacion en que se dispara una alerta
    # (ej. "90,60,30" -> alerta a 90, 60 y 30 dias del fin del contrato).
    PPA_ALERT_DAYS: str = "90,60,30"
    # Destinatarios (separados por coma) de las alertas de vencimiento de
    # contratos -- PPA (app/jobs/ppa_expiration_checker.py) y Representacion/CGM
    # (app/main.py._scheduled_representacion_alertas) comparten el mismo grupo
    # a proposito (confirmado con Sara, 2026-08-25). Antes hardcodeado por
    # duplicado en ambos archivos.
    PPA_ALERT_EMAILS: str = "adhara@unergy.io,jessica@unergy.io"

    # EVO Energy API (DailySpot + Clima via Tailscale)
    EVO_API_URL: str = ""
    EVO_API_TOKEN: str = ""

    # SMTP — envío de informes aprobados
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "operaciones@unergy.io"
    # Copia oculta (BCC) del Reporte CGM -- lista separada por comas.
    CORREO_SEGUIMIENTO: str = ""
    # Aviso inmediato a UN solo correo cuando CUALQUIER envio (reset password,
    # informe, alarma, falla, reporte CGM, prueba) falla -- mismo destinatario
    # sin importar el tipo. Best effort via el mismo SMTP, asi que si la cuenta
    # esta caida esta alerta puede fallar tambien (ver _alertar_fallo_envio).
    # Deliberadamente NO reusa CORREO_SEGUIMIENTO/SMTP_FROM: debe ser un correo
    # que alguien revise activamente, no la bandeja operativa que puede ser la
    # que esta fallando.
    ALERTA_FALLOS_EMAIL: str = ""

    # IMAP — lectura automática de correos entrantes (ej. Excel de terceros
    # que envía Cedillanos vía cgm@erco.energy, ver excel_terceros_email.py).
    # Reusa SMTP_USER/SMTP_PASSWORD -- misma cuenta de Gmail, el mismo App
    # Password sirve para IMAP y SMTP a la vez. Requiere que IMAP esté
    # habilitado en la configuración de esa cuenta de Gmail/Workspace.
    IMAP_HOST: str = "imap.gmail.com"
    IMAP_PORT: int = 993

    # IMAP de mandatos -- buzón adhara@unergy.io, el único en copia de las tres
    # fuentes de correo de mandatos (revisoría y envíos a inversionistas).
    # NO reusa SMTP_USER/SMTP_PASSWORD: esas son de operaciones@, otra cuenta.
    # Requiere App Password propio (verificación en dos pasos activa en la cuenta).
    MANDATOS_IMAP_USER: str = ""
    MANDATOS_IMAP_PASSWORD: str = ""
    # Segundo buzón, opcional. Parte del correo de mandatos no pasa por
    # adhara@: Jessica manda algunos a la revisoría desde su propia cuenta, y
    # esos viven en SU carpeta de Enviados. Sin leerlos, la reconciliación no
    # puede saber que esos mandatos salieron. Si queda vacío, se lee un solo
    # buzón y todo funciona igual, solo con menos cobertura.
    MANDATOS_IMAP_USER_2: str = ""
    MANDATOS_IMAP_PASSWORD_2: str = ""

    @model_validator(mode="after")
    def armar_database_url(self):
        # Sin DATABASE_URL, la URL se arma con las piezas POSTGRES_*/PG_*. Se
        # hace aca y no en cada consumidor para que la app, Alembic y los
        # scripts vean exactamente la misma cadena.
        if not self.DATABASE_URL:
            usuario = quote_plus(self.POSTGRES_USER)
            clave = quote_plus(self.POSTGRES_PASSWORD)
            self.DATABASE_URL = (
                f"postgresql+psycopg://{usuario}:{clave}"
                f"@{self.PG_HOST}:{self.PG_PORT}/{self.POSTGRES_DB}"
            )
        return self

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def fix_db_url(cls, v: str) -> str:
        # Los proveedores gestionados entregan postgres:// o postgresql://,
        # psycopg3 necesita postgresql+psycopg://
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+psycopg://", 1)
        if v.startswith("postgresql://") and "+psycopg" not in v:
            return v.replace("postgresql://", "postgresql+psycopg://", 1)
        return v


settings = Settings()
