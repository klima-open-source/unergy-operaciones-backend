# Plataforma Operaciones Unergy — Backend

## Qué es esto
API REST para la plataforma interna de operaciones de Unergy. Gestiona proyectos
solares, clientes, contratos/servicios, fallas, generación, monitoreo, liquidaciones,
garantías, alertas, fronteras (CGM) y el módulo MEM. Integra varias APIs externas
(Unergy, Solenium/SunFactory, Quoia/Gaia, EVO) y corre tareas en segundo plano (MGS).

## Stack
- **Python 3.12** + **FastAPI 0.115**
- **SQLAlchemy 2.0** (ORM, estilo `Mapped`/`mapped_column`) + **psycopg3** (driver PostgreSQL)
- **Pydantic v2** + **pydantic-settings** (schemas y configuración por entorno)
- **python-jose** (JWT) + **bcrypt** (contraseñas)
- **APScheduler** (jobs en background: polling de alarmas MGS)
- **httpx** (cliente HTTP hacia APIs externas)
- **openpyxl** (Excel: informes/liquidaciones), **matplotlib** + **numpy** (gráficas en informes)
- **google-api-python-client** / **google-auth** (integración Google)
- **Alembic** presente, pero las migraciones reales se hacen con DDL idempotente (ver abajo)
- **PostgreSQL 15** (Railway en producción, local en desarrollo)

## Estructura de carpetas
```
app/
├── main.py              # FastAPI app, CORS, lifespan, DDL idempotente al arranque,
│                        #   scheduler MGS, mounts estáticos (/static/uploads, /monitoreo), /health
├── core/
│   ├── config.py        # Settings (pydantic-settings): env vars, normaliza DATABASE_URL
│   ├── database.py      # Engine SQLAlchemy + SessionLocal + get_db()
│   └── security.py      # hash_password, verify_password, create_access_token, decode_token (HS256)
├── api/v1/
│   ├── router.py        # Registra TODOS los routers bajo prefix /api/v1
│   ├── auth.py          # POST /auth/token, GET /me, forgot/reset password, get_current_user(),
│   │                    #   usuarios_router (admin), soporta JWT y API Keys
│   ├── api_keys.py      # gestión de API keys
│   ├── dashboard.py · clientes.py · proyectos.py · portafolios.py
│   ├── fallas.py · generacion.py · generacion_solar.py · solar.py · monitoreo.py
│   ├── liquidaciones.py · garantias.py · representacion.py · om.py
│   ├── ppa.py · asic.py · fronteras.py · cumplimiento.py · mgs.py · reconectadores.py
│   ├── contratos_servicio.py · informes.py · notificaciones.py · mapa.py · proximos_energizar.py
│   └── evo_proxy.py · correlation.py     # proxies/lecturas a fuentes externas
├── models/              # Tablas SQLAlchemy 2.0 (un archivo por dominio)
│   ├── base.py          # Base (DeclarativeBase) + TimestampMixin (created_at/updated_at)
│   ├── usuarios.py      # Usuario, RolEnum
│   ├── clientes.py · proyectos.py · contratos.py · servicios.py
│   ├── fallas.py · generacion.py · om.py · mantenimientos.py · equipos.py
│   ├── liquidaciones.py · garantias.py · cumplimiento.py · fronteras.py
│   ├── asic.py · rec.py · promotor.py · documentos.py · informes.py
│   ├── gestion.py · notificaciones.py
│   └── ...
├── schemas/             # Pydantic schemas (request/response), un archivo por dominio
│   ├── common.py        # PaginatedResponse[T]
│   ├── usuarios.py · clientes.py · proyectos.py · fallas.py · contratos_servicio.py
│   └── cumplimiento.py · fronteras.py · garantias.py · generacion.py · mgs.py · notificaciones.py · om.py · ppa.py · asic.py
├── services/
│   ├── audit.py             # registro de auditoría
│   ├── correlation.py       # cruces con BD externas (origina / requests)
│   ├── email_service.py     # envío SMTP de informes aprobados
│   ├── om_calculator.py     # cálculos de O&M
│   └── mgs/                 # cliente + scheduler de alarmas MGS
├── utils/
│   ├── liquidaciones_loader.py   # carga de Excel de liquidaciones
│   └── proyecto_matching.py      # matching de nombres de proyecto
└── seeds/
    └── seed_data.py     # usuarios iniciales + catálogos (fallas, etc.)

alembic/                 # 79 revisiones, un solo head; start.sh aplica upgrade head
init_db.py               # crea tablas + add_columns() (ALTER idempotente) + seed
```

## Migraciones de BD (importante)
**Corrección (2026-08-24): el proyecto SÍ usa Alembic.** Hay un solo head y `start.sh` corre
`alembic upgrade head` en cada deploy; las revisiones 035-075 son trabajo real. Lo que sigue
describe el DDL idempotente, que convive con Alembic y corre **antes** que él:

- **Columna nueva en tabla existente** → agregar un `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
  en `init_db.py` → `add_columns()`. (Para columnas críticas en producción también se
  añaden a la lista `_PENDING_DDLS` de `app/main.py`, que se ejecuta en cada arranque.)
- **Tabla nueva** → definir el modelo en `app/models/` y dejar que
  `Base.metadata.create_all()` la cree (corre en `init_db.py` y al arranque).
- Los `ALTER ... IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS` son seguros de re-ejecutar.

> **Escribe las revisiones con los helpers de `alembic_idempotencia.py`** (`tabla_existe`,
> `columna_existe`, `agregar_columna_si_falta`, …). Como el DDL idempotente corre antes,
> una revisión que asuma que un objeto no existe puede fallar — y al ir todo el
> `upgrade head` en una transacción, ese fallo revierte la cadena completa.
> `--autogenerate` sirve como borrador, pero revisa la salida: no conoce `_PENDING_DDLS`.

## Cómo agregar un nuevo endpoint

### 1. Modelo (si la tabla no existe)
```python
# app/models/mi_modelo.py
from sqlalchemy import BigInteger, String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin

class MiModelo(Base, TimestampMixin):
    __tablename__ = "mi_tabla"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nombre: Mapped[str] = mapped_column(String, nullable=False)
    proyecto_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("proyectos.id"))
```
Importar el modelo en `app/models/__init__.py` para que `create_all` lo registre.

### 2. Schema Pydantic
```python
# app/schemas/mi_modelo.py
from pydantic import BaseModel

class MiModeloCreate(BaseModel):
    nombre: str
    proyecto_id: int

class MiModeloOut(MiModeloCreate):
    id: int
    model_config = {"from_attributes": True}
```

### 3. Router
```python
# app/api/v1/mi_modulo.py
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.api.v1.auth import get_current_user
from app.models.mi_modelo import MiModelo
from app.schemas.mi_modelo import MiModeloCreate, MiModeloOut

router = APIRouter(prefix="/mi-modulo", tags=["Mi Módulo"])

@router.get("/", response_model=list[MiModeloOut])
def listar(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return db.query(MiModelo).all()

@router.post("/", response_model=MiModeloOut)
def crear(data: MiModeloCreate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    obj = MiModelo(**data.model_dump())
    db.add(obj); db.commit(); db.refresh(obj)
    return obj
```

### 4. Registrar en `app/api/v1/router.py`
```python
from app.api.v1 import mi_modulo
api_router.include_router(mi_modulo.router)
```

### 5. Migración (si agregaste tabla/columna)
Ver sección **Migraciones de BD** arriba. Tabla nueva → `create_all` la crea sola.
Columna nueva → `ALTER ... IF NOT EXISTS` en `init_db.py` (y `_PENDING_DDLS` si es prod).

## Autenticación
- Login: `POST /api/v1/auth/token` (form-urlencoded: `username`, `password`) → `access_token` JWT.
- Todos los endpoints protegidos llevan `_=Depends(get_current_user)`.
- El token va en el header `Authorization: Bearer <token>`.
- `get_current_user` acepta **JWT** o **API Key** (`api_keys.py`), y valida que el usuario esté activo.
- Hay flujo de **forgot/reset password** (`/auth/forgot-password`, `/auth/reset-password`).

## Roles disponibles
`admin` · `operaciones` · `monitoreo` · `liquidaciones` · `cgm` · `solo_lectura`
(definidos en `RolEnum`, `app/models/usuarios.py`).

Para restringir por rol:
```python
from fastapi import Depends, HTTPException, status
from app.api.v1.auth import get_current_user
from app.models.usuarios import Usuario, RolEnum

def solo_admin(current: Usuario = Depends(get_current_user)):
    if current.rol != RolEnum.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Se requiere rol admin")
    return current
```

## Integraciones externas (vía `httpx`)
Configuradas en `config.py` y consumidas por routers/servicios:
- **API Unergy** (`UNERGY_*`) — bridge `_legacy` para datos de monitoreo/generación reales.
- **SunFactory / Solenium** (`SUNFACTORY_*`, `SOLENIUM_*`) — cronogramas EPC (próximos a
  energizarse) y datos de inversores FMO; auth OAuth2 username/password.
- **Quoia / Gaia** (`QUOIA_*`, `GAIA_*`) — fronteras CGM y medidores.
- **EVO Energy** (`EVO_*`) — DailySpot diario, solo la tarea `ingesta_bolsa` (vía Tailscale).
- **SMTP** (`SMTP_*`) — envío de informes mensuales aprobados.
- **BD externas read-only** (`ORIGINA_DATABASE_URL`, `REQUESTSDB_DATABASE_URL`) — usadas
  por `services/correlation.py` para cruces de datos.

## Tareas en background — MGS
`app/services/mgs/` corre con **APScheduler** (arranca en el `lifespan` de `main.py`):
hace polling de alarmas cada `MGS_POLL_INTERVAL_MINUTES` (default 15) más jobs cron.
Se controla con `MGS_ENABLED` y `TIMEZONE` (America/Bogota).

## Almacenamiento de archivos
`STORAGE_BACKEND` = `local` (default, sirve en `/static/uploads`) o `s3`
(`S3_BUCKET`/`S3_ENDPOINT`/`S3_ACCESS_KEY`/`S3_SECRET_KEY`).

## Variables de entorno (.env en la raíz del backend)
```
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/operaciones
SECRET_KEY=<clave-segura-min-16-chars>
JWT_EXPIRE_MINUTES=480
ENVIRONMENT=development
FRONTEND_URL=http://localhost:5173

# Almacenamiento
STORAGE_BACKEND=local
STORAGE_LOCAL_PATH=./uploads

# Integraciones (opcionales en local; requeridas en prod según el módulo)
UNERGY_API_URL=https://api.unergy.io
UNERGY_ACCOUNT_ID=  UNERGY_LOGIN=  UNERGY_PASSWORD=
SUNFACTORY_USERNAME=  SUNFACTORY_PASSWORD=
SOLENIUM_USER=  SOLENIUM_PASS=
QUOIA_API_TOKEN=  GAIA_USER=  GAIA_PASS=
EVO_API_URL=  EVO_API_TOKEN=
ORIGINA_DATABASE_URL=  REQUESTSDB_DATABASE_URL=
SMTP_HOST=  SMTP_USER=  SMTP_PASSWORD=  SMTP_FROM=operaciones@unergy.io

# MGS scheduler
MGS_ENABLED=true
MGS_POLL_INTERVAL_MINUTES=15
TIMEZONE=America/Bogota
```
> `DATABASE_URL` se normaliza solo: `postgres://` y `postgresql://` se convierten a
> `postgresql+psycopg://` (Railway entrega el formato corto).

## Correr localmente
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python init_db.py      # crea tablas, aplica add_columns() y seed inicial
uvicorn app.main:app --reload
# API en http://localhost:8000  ·  Docs en http://localhost:8000/docs  ·  Health: /health
```

## Producción
- **Plataforma:** Railway
- **Deploy:** automático al hacer push a `master` en GitHub
- **URL backend:** https://backend-production-63d8.up.railway.app
- **Docs producción:** https://backend-production-63d8.up.railway.app/docs

## Convenciones
- Un archivo por dominio en `app/models/`, `app/schemas/` y `app/api/v1/`.
- Modelos en estilo SQLAlchemy 2.0: `Mapped[...]` + `mapped_column`, heredando `TimestampMixin`.
- **IDs `BigInteger` autoincrement** (BIGSERIAL) — no UUID.
- Siempre usar `response_model` en los endpoints.
- Todo endpoint protegido lleva `Depends(get_current_user)`.
- Migraciones por DDL idempotente (`init_db.py` / `_PENDING_DDLS`), no autogenerate de Alembic.
- No usar `print()` para logs de aplicación — usar el logger de uvicorn (los `print` de
  `[startup]`/`[shutdown]` en `main.py` son trazas de arranque intencionales).
