# unergy-operaciones-backend

Backend **Django + DRF** de la plataforma de Operaciones de Unergy. **Django es la
única forma de escribir código acá.** Base de datos `operations` en PostgreSQL,
externa al despliegue (`POSTGRES_*`/`PG_*` en el `.env`). Corre con
`gunicorn config.wsgi:application`, más Celery (`worker` + `beat`) para las tareas
programadas. Se despliega con `docker compose up -d --build` en el servidor, y
automáticamente en cada push a `master` (`.github/workflows/deploy.yml`). Cómo se
construye: `README.md`.

## La regla dura: dónde va el código

Django reemplazó a FastAPI el 2026-09-04. Lo que corre en producción es
`config/` + `apps/` + `api/`.

| Qué estás escribiendo | Dónde va |
|---|---|
| Modelos, lógica de negocio, tareas | `apps/<dominio>/` — `models.py`, `services/`, `tasks/` |
| Endpoints | `api/v1/<recurso>/` — `urls.py`, `views.py`, `serializers.py` |
| Cambios de esquema | una migración de Django en `apps/<app>/migrations/` |
| Una franja horaria para una tarea | `config/horarios.py` |

**Prohibido**, sin excepciones:

- agregar un router o un `APIRouter` de FastAPI,
- declarar un modelo de SQLAlchemy,
- escribir una revisión de Alembic.

`tests/test_solo_django.py` recorre los `.py` de `apps/`, `api/` y `config/` y
falla si alguno importa `fastapi` o `sqlalchemy`. No es prosa: es un test.

**Qué es `app/`.** Es el árbol FastAPI **apagado**. Sigue en la imagen por una
sola razón: `apps/` todavía le importa 21 módulos de integración —los clientes de
MGS (Gaia, SolarView, Solenium), el `AlarmEngine`, SMTP, los parsers de correo de
mandatos, `liquidaciones_loader`. Se lee, no se extiende. **Un archivo nuevo en
`app/` es un error, no una opción.** Tiene 266 archivos y parece vivo; no lo está,
nada de ahí se sirve.

Portar esos 21 módulos (y reapuntar los tests que miran `app/`) es otro proyecto,
no una tarea que se cuele en un cambio de negocio.

## Por dónde empezar según la tarea

| Si vas a tocar… | Lee primero |
|---|---|
| Cualquier cosa: la estructura de los dos árboles | `apps/README.md` |
| Portar un módulo, o las convenciones de dominio/HTTP | `migration.md` (referencia canónica) |
| A qué app de Django va una tabla, un router o un servicio | `docs/DOMINIOS.md` |
| Fallas, monitoreo solar o informes mensuales | `docs/ARQUITECTURA_MONITOREO.md` |
| Cualquier base de datos de Unergy | `docs/UNERGY_DATABASE_ATLAS.md` (6 bases, 511 tablas) |
| Integridad o higiene de datos | `docs/DB_REVIEW_TEAM.md` |
| Un endpoint concreto | `docs/API_*.md` — hay uno por API |
| El build, el compose o el despliegue | `README.md` |

## Cosas que cuesta descubrir solo

**El repo local suele estar atrasado.** Antes de analizar nada:

```bash
git fetch origin && git rev-list --left-right --count master...origin/master
```

Si el segundo número no es 0, lo que estás leyendo no es lo que corre en producción.

**El working tree puede traer trabajo de otra persona.** Revisa `git status` y el
conteo del diff antes de commitear, o desplegarás cambios ajenos.

**El esquema es de Django, y el deploy lo verifica.** El servicio `migrate` del
compose es one-shot y sin `||`: corre `manage.py migrate --fake-initial`,
`showmigrations --plan` y después `scripts/verificar_esquema_django.py`. Ese último
compara los modelos contra la base y **falla el deploy** si un modelo declara una
columna, una tabla o un tipo que la base no tiene. Si falla, `operaciones` no
levanta (`depends_on: service_completed_successfully`) — antes el arranque
toleraba el fallo y la app quedaba sirviendo 500 con el esquema atrasado. Si
alguna vez le agregas un `||` a ese `command`, estás reintroduciendo ese modo de
fallo.

**`--fake-initial` está puesto a propósito y hay que quitarlo.** Es lo que permite
adoptar una base que YA tiene sus 119 tablas: marca aplicada cada migración
`initial` cuyas tablas existen. Sirve exactamente una vez. Dejarlo cambia un fallo
ruidoso por uno callado — una migración `initial` nueva que choque con una tabla
existente queda marcada como aplicada SIN crear nada, y el modelo pierde sus
columnas en silencio. El razonamiento completo y el procedimiento para retirarlo
están en el comentario del servicio `migrate` en `docker-compose.yml`.

**Alembic está congelado.** La revisión 143 es la última; `alembic/` se conserva
como bitácora histórica y **no se ejecuta en ningún lado**. `git log` sobre esa
carpeta sigue siendo la forma de averiguar cuándo apareció una columna vieja.

**Las tareas programadas son Celery, no un scheduler en proceso.** Las 19 franjas
viven en `config/horarios.py` (fuente de verdad, versionada); `beat` las dispara y
`worker` las corre. `django_celery_beat` guarda el horario en Postgres, pero el
`DatabaseScheduler` sincroniza el diccionario al arrancar: editar una franja desde
el admin funciona hasta el próximo reinicio. Todo en hora de Bogotá
(`CELERY_TIMEZONE = America/Bogota`) aunque el contenedor corra en UTC.

**`WORKERS` ya puede ser >1.** La restricción existía porque el
`BackgroundScheduler` vivía dentro del proceso web y cada worker arrancaba el suyo,
duplicando los jobs. Eso ahora es el servicio `worker` y la restricción desapareció.
Lo que **sí** sigue aplicando es `--concurrency=1` en el `worker`: dos tareas
guardan estado en memoria del proceso (`monitoreo.sondeo_mgs` y
`mandatos.revisar_correos`) y fallan calladas si se reparten. Ver el comentario de
`worker` en el compose antes de tocarlo.

**Dónde va cada cambio.** Esquema (CREATE/ALTER/índice/constraint) = migración de
Django en `apps/<app>/migrations/`. Datos (backfill, migración de filas) = un
management command en `apps/<app>/management/commands/` o `manage.py shell`,
corrido a mano una vez y borrado; **nunca dentro de una migración** — una migración
de datos que falle deja el deploy a medias y su error se pierde.

Las tareas `*_seed` de `_deferred_init` (`app/main.py`) siguen escritas pero **no
corren**: eran del arranque de FastAPI, y ese arranque ya no ocurre. No agregues
nada ahí.

**Conexión a la base.** Se arma en `config/settings.py::DATABASES` desde las cinco
`POSTGRES_*`/`PG_*`, y `DATABASE_URL` las pisa si está puesta. Las bases externas
de otros servicios (`REQUESTSDB_DATABASE_URL`, `ORIGINA_DATABASE_URL`) van por
psycopg crudo y **no** están en `DATABASES` a propósito: su esquema no es nuestro y
no se les aplica ninguna migración.

**El código está montado, no copiado.** El compose monta el repo en `/app`, así que
un cambio de Python entra con `docker compose restart operaciones` (y `worker`/`beat`
si tocaste tareas); solo hace falta `--build` si cambió `pyproject.toml`, `uv.lock`
o el `Dockerfile`. Por eso el venv de la imagen vive en `/opt/venv` y no en
`/app/.venv`: el bind mount lo taparía.

**Dependencias con uv, y `uv.lock` se commitea.** El `Dockerfile` corre
`uv sync --frozen`, que falla si el lock no cuadra con el `pyproject.toml`. `uv add
<paquete>` actualiza los dos; van en el mismo commit o el build se cae.

**Producción no se escribe desde local.** El `.env` local no apunta a producción, y
el del servidor se reescribe en cada deploy desde el secret `ENV_FILE` (editarlo por
SSH no sirve). Para tocar datos de producción: un management command commiteado, o
`docker compose exec operaciones python manage.py shell` en el servidor.

**Zona horaria.** Colombia es UTC−5 sin horario de verano y el contenedor corre en
UTC. Usa `apps.plataforma.services.fechas.hoy_col()`, no `date.today()`: entre las
19:00 y medianoche de Bogotá el servidor ya está en el día siguiente.

**Fuente de verdad del esquema.** Ante cualquier duda sobre una columna o una clave
foránea, gana el DDL: `esquema-bd-produccion/esquema_produccion.sql` (está en la raíz
del workspace, fuera de este repo). Los documentos derivados pueden tener errores —
`DEPURACION.md`, por ejemplo, afirma que `mantenimiento_impacto.falla_id` no tiene
clave foránea, y sí la tiene.

## Pruebas

```bash
uv sync
uv run pytest -q
```

Deben pasar todas antes de subir. Al 7 de septiembre de 2026: 2 725 pruebas
(4 skipped).
