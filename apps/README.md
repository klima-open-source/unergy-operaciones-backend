# Cómo se porta un módulo a Django

Estructura y convenciones: `migration.md` (referencia canónica de Origina).
Qué tabla va a qué app: `docs/DOMINIOS.md`. Este archivo cubre solo lo que es
**distinto acá** y el procedimiento concreto.

## Los dos árboles, y dónde viven

```
apps/<dominio>/          ← árbol A: dominio. No sabe que existe HTTP.
  apps.py models.py services/ tasks/ migrations/
api/v1/<recurso>/        ← árbol B: HTTP. No calcula nada.
  urls.py views.py serializers.py queryset.py tests.py
api/                     ← capa compartida: fields, permissions, pagination,
                           authentication, logging
config/                  ← proyecto Django: settings, urls, wsgi, celery
```

Origina tiene sus ~48 apps planas en la raíz. Acá van bajo `apps/` porque son
115 tablas y una raíz con 18 directorios de dominio mezclados con `alembic/`,
`scripts/`, `docs/` y `data/` no se navega. El costo es una línea por app:
`name = "apps.<dominio>"` y `label = "<dominio>"` en el `AppConfig` — el `label`
explícito evita que el app_label quede como `apps_retos` y que las migraciones
lo arrastren.

Import por alias, como en Origina: `from apps.retos import models as rt_models`.
Nunca `from apps.retos.models import RetoMetrica`.

## Las cuatro reglas propias de este repo

**1. Alembic sigue siendo el dueño del esquema. `managed = False` en todos los
modelos.** Mientras FastAPI siga leyendo estas tablas solo puede haber un dueño,
y ya es Alembic (`CLAUDE.md`: "Alembic es el ÚNICO camino para el esquema"). Un
`makemigrations` que emita DDL sobre una tabla que Alembic también controla es
cómo se pierde una columna un viernes.

*Cuándo se invierte:* cuando el último lector FastAPI de las tablas de una app
desaparece. Ahí, y solo para esa app: quitar `managed = False`,
`makemigrations`, y `migrate --fake-initial` para que Django adopte el esquema
existente sin recrearlo.

**2. Las URLs no cambian. Nunca.** El frontend en producción llama
`/api/v1/<recurso>` y no debe enterarse de qué backend responde. Dos cosas lo
sostienen y las dos son fáciles de romper:

- `DefaultRouter(trailing_slash=False)` en cada `urls.py` de recurso, y
  `APPEND_SLASH = False` en settings. Sin eso Django redirige `/retos` a
  `/retos/` con un 301 que en un POST **pierde el cuerpo**.
- El orden de `router.register()`. Un prefijo literal que colisiona con el
  lookup de detalle (`retos/metricas` contra `retos/{pk}`) se registra **antes**,
  o el `[^/.]+` del pk se traga la palabra.

`tests/test_paridad_urls.py` compara las dos tablas de rutas y falla si divergen.
Al portar un módulo se agrega su prefijo a `PREFIJOS_PORTADOS` y queda vigilado.

**3. `log_endpoint` re-lanza la excepción.** El de Origina la captura y devuelve
una respuesta RFC 9457 en su lugar — eso cambiaría el formato de error de los
493 endpoints que el frontend ya consume. Acá se registra y se re-lanza; el
cuerpo lo decide el manejador de DRF. No copiar ese comportamiento sin cambiar
antes el contrato de error.

**4. La paginación devuelve `{items, total, page, size}`,** no el
`{count, next, previous, results}` de DRF. Cambiar los nombres de las claves
rompe el frontend igual que cambiar una ruta. Está en `api/pagination.py`.

## El procedimiento, de adentro hacia afuera

1. **`apps/<dominio>/models.py`** — `TextChoices` para todo estado,
   `verbose_name` en cada campo, `managed = False` + `db_table` en el `Meta`.
   Heredar `Timer` de `apps.plataforma.models` para los timestamps.
2. **`apps/<dominio>/services/`** — la lógica de negocio. Si ya está separada en
   `app/services/`, suele moverse sin reescribir: el servicio de `retos` (265
   líneas) se copió tal cual porque es cálculo puro sin sesión de base.
3. **`api/v1/<recurso>/queryset.py`** — funciones a nivel de módulo que reciben
   ids. Las consultas con sus `prefetch_related`, y las funciones `build_*` que
   anotan lo calculado sobre las instancias.
4. **`api/v1/<recurso>/serializers.py`** — uno por dirección (lectura/escritura)
   y uno por granularidad. El detalle **subclasifica** al resumen.
5. **`api/v1/<recurso>/views.py`** — mixins explícitos (nunca `ModelViewSet` por
   si acaso), `http_method_names` para cerrar verbos, `permission_classes` +
   `required_role`, `@class_logger_wrapper`, y el contrato en el docstring.
6. **`api/v1/<recurso>/urls.py`** — `DefaultRouter(trailing_slash=False)`,
   `basename` único.
7. **Registrar**: el `include()` en `api/v1/urls.py`, la app en `INSTALLED_APPS`,
   el prefijo en `PREFIJOS_PORTADOS` de `tests/test_paridad_urls.py`.
8. **Revisar**: ¿la vista calcula? ¿el serializer consulta? ¿el queryset
   escribe? ¿el servicio de dominio importa algo de DRF? Cualquier "sí" es
   código en la capa equivocada.

## Decisiones tomadas — no reabrir sin leer esto

**`cachalot` queda APAGADO durante toda la coexistencia.** Origina lo usa
(`CACHALOT_TIMEOUT = 300`) y la tentación de copiarlo es obvia. No acá, y el
motivo no es de rendimiento: cachalot invalida su caché observando las
escrituras que pasan por el cursor de Django. Mientras FastAPI siga escribiendo
estas 115 tablas, **esas escrituras son invisibles para el proceso Django**, que
seguirá sirviendo lecturas viejas hasta que expire el timeout. Es un bug de
corrección que ninguna prueba puede atrapar, porque en pruebas Django es el
único que escribe. Se enciende el día que Django sea el único escritor.

Dato de Origina que conviene conocer antes de encenderlo allá o acá: lo
apagaron el 2026-03-03 en un commit titulado `fix: deactivate cachalot` y lo
reencendieron dos días después dentro de un commit de infraestructura no
relacionado. **Nadie registró la causa.** Si se enciende, que sea con
`CACHALOT_TIMEOUT` corto: el default es sin expiración, así que una invalidación
perdida es permanente hasta el reinicio.

**Un `serializers.py` por recurso, nunca un paquete `serializers/`.** Origina
tiene las dos formas —10 recursos con archivo, 11 con paquete— después de años y
veinte recursos; es la pregunta que ese repo no resolvió. Acá se resuelve ahora:
archivo único, granularidad por nombre de clase (`RetoResumenSerializer`,
`RetoDetalleSerializer`). Si un recurso llega a necesitar el paquete, esa es una
decisión con su propio commit y su motivo escrito.

**Django ya posee tablas en esta base: son su isla.** La regla 1 dice que
Alembic posee el esquema, y eso es cierto para las 115 tablas de dominio. Pero
`contenttypes` y `django_celery_beat` traen 7 tablas propias (más
`django_migrations`), y `manage.py migrate` las crea. La pregunta útil no es
«¿hacemos una excepción a Alembic?» sino **«qué tablas están en la isla de
Django?»**, y la respuesta es: las que no tienen ninguna arista hacia el
esquema de dominio.

    tests/test_frontera_esquema.py verifica las dos mitades: que ningún modelo
    de dominio sea `managed`, y que la isla sea exactamente la lista conocida.

**`django_tracker` (auditoría declarativa) cabe en la isla.** Su `AuditLog`
llega al objeto auditado por `GenericForeignKey` — `content_type` más un
`object_id` que es `CharField`, no una clave foránea. **No hay ninguna
referencia a nivel de base contra las tablas de dominio**: no hay restricción
que violar, ni orden que respetar entre una revisión de Alembic y una migración
de Django, ni nada que se rompa si Alembic reforma una tabla auditada.

Por eso lo posee Django y no Alembic: trae sus propias migraciones (tres, con
cinco índices) y transcribirlas a mano —y volver a transcribirlas en cada
actualización del paquete— sería trabajo recurrente para una tabla que FastAPI
no lee nunca. Al instalarlo se agrega a la lista de la isla en
`tests/test_frontera_esquema.py`.

Es un paquete interno de Unergy (`django-tracker-unergy`,
`gitlab.com/unergy-dev/origination/django-tracker`), así que reemplazar el
`app/services/audit.py` imperativo por `@auditable(...)` sobre el modelo es un
`uv add`. Dos detalles al instalarlo: `CurrentUserMiddleware` va **después** del
middleware que puebla `request.user`, o cada fila queda anónima; y `@auditable`
debe excluir `created_at`/`updated_at`, porque con `auto_now` cada guardado
produce un diff que no dice nada.

No copiar de Origina el correr `easyaudit` en paralelo con casi todo apagado.
Un sistema de auditoría, no dos.

## Los modelos se generan, no se transcriben

`scripts/generar_modelos_django.py` lee los metadatos de SQLAlchemy
(`Base.metadata`) y escribe `apps/<dominio>/models.py`. Son 118 tablas y ~1200
columnas: transcribirlas a mano es garantizar erratas silenciosas — un
`nullable` invertido no falla al arrancar, falla el día que alguien guarda un
nulo.

```bash
PYTHONPATH=. uv run python scripts/generar_modelos_django.py --listar
PYTHONPATH=. uv run python scripts/generar_modelos_django.py proyectos clientes
```

Lo normal sería `manage.py inspectdb`, pero exige una base viva y el `.env`
local no apunta a ninguna. Los metadatos son la mejor fuente disponible, y son
exactos en tipo, nulabilidad, claves foráneas, índices y restricciones únicas.

`--listar` verifica el mapa de dominios: avisa si una tabla está en dos
dominios o si alguna quedó sin asignar. Así aparecieron tres tablas de
asociación que `docs/DOMINIOS.md` no listaba.

**Lo generado es un borrador.** Acierta la estructura; no puede inventar el
`verbose_name` en español, los `TextChoices` de las columnas de estado, los
docstrings del modelo de datos, ni un `related_name` legible. El generador emite
`related_name="<tabla>_por_<columna>"`, que es feo pero **único** — necesario
donde hay dos FK a la misma tabla (`asic_cambios_contratos` apunta dos veces a
`proyectos`). Se pulen los de las relaciones que un recurso recorra de verdad,
cuando se porte ese recurso, no antes.

### Dos cosas que el generador NO hacía, y ahora sí

**Los `server_default` se perdían.** SQLAlchemy declara los valores por defecto
en PostgreSQL (`server_default="false"`, `server_default=func.now()`) y no los
manda en el INSERT; Django manda **todas** las columnas, así que un default que
solo vive en la base nunca se usa: la columna viaja como `NULL` y la fila
revienta si es `NOT NULL`. Salió al crear un registro CND —
`registro_etapa.fecha_estado`. `valor_por_defecto()` los traduce ahora a
`default=` de Django: 94 columnas en 18 dominios los tenían.

**Regenerar `plataforma` borraba `Timer` y `Rol`.** Ese archivo se mantiene a
mano — las dos clases no salen de ningún metadato — y una corrida de
`generar_modelos_django.py <todos>` lo dejó sin ellas: 65 pruebas en rojo y el
modelo `ApiKey` (que tampoco tiene tabla en SQLAlchemy) perdido. El generador
ahora **omite cualquier `models.py` que no lleve la marca `GENERADO por`** en su
docstring. Si escribes a mano un modelo dentro de un archivo generado, o lo
mueves a otro archivo, o le quitas la marca — pero entonces ese dominio deja de
regenerarse entero.

## Hay 28 tablas en producción SIN modelo

`scripts/generar_modelos_django.py` cubre las 118 tablas que tienen modelo
SQLAlchemy. **No son todas las tablas de la base.** Otras 28 se crearon desde
`_PENDING_DDLS` (retirada el 2026-08-31), quedaron capturadas en la revisión 135
de Alembic y nunca tuvieron modelo: el código las consulta con SQL crudo
(`text("SELECT * FROM api_keys …")`). Son invisibles para el ORM y para
`scripts/verificar_esquema.py`.

De esas 28, **10 se usan de verdad** (aparecen en un `FROM`/`INSERT`/`UPDATE`):

| Tabla | Usos en SQL | Dominio | Estado |
|---|---|---|---|
| `api_keys` | 12 | `plataforma` | ✅ modelo declarado, recurso portado |
| `precios_bolsa_diario` | 6 | `mercado_xm` | pendiente |
| `clima_oni_monthly` | 4 | `energia` (evo_proxy) | pendiente |
| `email_envios` | 3 | `plataforma` | pendiente |
| `clima_price_monthly` | 3 | `energia` | pendiente |
| `clima_precip_monthly` | 3 | `energia` | pendiente |
| `clima_forecasts` | 3 | `energia` | pendiente |
| `precios_bolsa_horario` | 2 | `mercado_xm` | pendiente |
| `alarma_estado` | 2 | `monitoreo` | pendiente |
| `audit_log` | 1 | `plataforma` | pendiente |

Las 18 restantes no aparecen en ningún SQL ni en ningún modelo —
`contratos_arriendo`, `equipos`, `equipos_sellos`, `documentos`, `garantias`,
`garantias_movimientos`, `fronteras_lecturas`, `gmail_credenciales`,
`liquidacion_xm_datos`, `monitoreo_verificaciones`, `operacion_kpis`,
`proyecto_grupos_panel`, `proyecto_inicio_operacion`, `rec_certificados`,
`reglas_contables`, `representacion_gescon`, `servicio_cgm`,
`correlation_sync_log`— y son candidatas a borrarse, pero eso es una decisión
aparte y necesita confirmarse contra la base antes de tocar nada.

**Al portar un recurso que use una de las 10, hay que declarar su modelo
primero**, transcribiendo del DDL de la revisión 135, que es la fuente de
verdad. El generador no puede ayudar: no hay metadatos de los que leer.

Ojo con el conteo de usos: buscar el nombre de la tabla a secas da falsos
positivos, porque `equipos`, `documentos` y `garantias` son palabras corrientes
en los comentarios en español. La cuenta de arriba solo mira los que van
después de `FROM`, `JOIN`, `INTO`, `UPDATE` o `TABLE`.

## Coexistencia mientras dure la migración

Los dos backends corren a la vez y un proxy en el host reparte por prefijo: los
prefijos portados van a Django, el resto a FastAPI. Una línea de `location` por
módulo migrado, la misma lista que `PREFIJOS_PORTADOS`.

No hay big-bang: 493 endpoints migrados de una vez es exactamente la clase de
despliegue que se revierte a las 3 de la mañana.





## El test de paridad estuvo 26 recursos pasando en vacío

Vale la pena contarlo porque es el fallo más caro de esta migración y no dejó
ninguna señal.

`tests/test_paridad_urls.py` estuvo en verde desde el primer recurso portado
**sin comparar nada**. Dos bugs superpuestos, ninguno visible:

1. **Lado FastAPI**: recorría `app.routes`. Esta versión de FastAPI NO aplana
   los routers incluidos —los envuelve en un `_IncludedRouter`—, así que ahí
   solo aparecen las cinco rutas de nivel raíz (`/docs`, `/health`…). Se leían 5
   rutas de 493. Ahora se leen del esquema OpenAPI (`app.openapi()["paths"]`),
   que es la vista pública y no depende de cómo la versión de turno guarde los
   routers por dentro.
2. **Lado Django**: al concatenar los patrones anidados quedaba
   `api/v1/^retos$`, con el `^` en medio. El filtro por prefijo descartaba
   entonces TODAS las rutas. Hay que quitar `^`/`$` **por segmento**, no del
   texto ya concatenado. (El mismo bug lo tuvo `test_resolucion_rutas.py`; ahí
   sí se detectó al escribirlo, porque las rutas no resolvían.)

Con los dos bugs, ambos conjuntos quedaban vacíos y `set() - set()` es vacío:
las dos aserciones pasaban sin mirar nada.

**Lo que lo hace no repetible**: el test ahora afirma que cada lado leyó al
menos una ruta antes de compararlas. Un test que filtra a vacío no falla nunca,
y esa es la lección general — cualquier prueba que compare dos conjuntos
derivados de un filtro necesita afirmar que el filtro no los vació.

**Qué encontró al arreglarlo**, sobre 27 recursos ya portados: solo dos
divergencias reales, las dos iguales. `mixins.UpdateModelMixin` registra PUT y
PATCH juntos, y en dos ViewSets donde solo se implementa `partial_update` el
PUT quedaba expuesto sin existir en FastAPI. La solución no es
`http_method_names` (eso da 405, que es lo mismo que hace FastAPI): es **no
incluir el mixin** cuando solo se implementa `partial_update`, porque el router
mapea por atributo existente, no por mixin.

## Quince endpoints devolvían 400 donde FastAPI devuelve 422

`ValidationError(mensaje, code=422)` **no** devuelve 422. En DRF el `code` es una
etiqueta del error; el status lo fija `status_code` de la clase, y el de
`ValidationError` es 400. Los quince sitios portados con ese patrón contradecían
el contrato de FastAPI (`HTTPException(422, …)`) sin que nada avisara: la
paridad compara rutas y verbos, no códigos de estado.

Se corrigió con `api.exceptions.NoProcesable` (422). **Es el mismo error que
espera a cualquier otro status que DRF no traiga de fábrica** — 409 ya tenía su
`Conflict`. Antes de portar un endpoint que devuelva algo distinto de
200/201/204/400/403/404, revisa qué status trae la excepción de DRF que ibas a
usar.

## El fallo que la paridad de URLs NO detecta

`tests/test_paridad_urls.py` compara CONJUNTOS de rutas: ve una que falta o una
de más. No ve que dos se solapen — y ese es el fallo silencioso de DRF.

**`DefaultRouter` ordena las `@action` ALFABÉTICAMENTE**, no por orden de
declaración: `get_extra_actions()` hace `sorted(...)`. Así que una acción
`documentos` con un `url_path` comodín se registra ANTES que `documentos_upload`
y se traga `/documentos/upload`. La ruta existe, resuelve, y atiende la vista
equivocada con un «período» llamado *upload*.

Pasó de verdad al portar `arriendos`. Por eso hay una segunda prueba,
`tests/test_resolucion_rutas.py`, con dos partes:

- Una tabla de casos AMBIGUOS concretos (ruta → acción que debe atenderla).
- `test_ninguna_ruta_con_comodin_tapa_una_ruta_literal`, que recorre TODAS las
  rutas literales de la API y comprueba que cada una resuelva a sí misma. Esa no
  hay que mantenerla: cubre sola cada recurso nuevo.

**La solución no es reordenar**, porque el orden no se controla. Es que cada
patrón acepte solo lo que de verdad es: `(?P<periodo>\d{4}-\d{2})` para un
período y `(?P<doc_id>\d+)` para un id, en vez de un `[\w-]+` que se come
cualquier cosa.

## `cumplimiento`: 3 805 líneas donde los servicios llamaban a una vista

Era el módulo que el mapa de dominios dejó para el final. El problema no era el
tamaño: era que **tres servicios lo consumían invocando un endpoint**.

```python
# app/services/vista_contratos.py, y también balance_energia y clasificacion_energia
from app.api.v1.cumplimiento import get_plantas_contratos
piscinas = get_plantas_contratos(year=year, month=month, db=db, _=None)
```

Ese `_=None` es el parámetro `current_user` de FastAPI: se le pasaba `None` para
saltarse la autenticación de una vista que en realidad nunca fue una vista. Los
imports estaban diferidos dentro de las funciones para romper el ciclo.

Al portar, `get_plantas_contratos` pasó a ser
`apps/mercado_xm/services/cumplimiento/piscinas.plantas_contratos()` y el ciclo
desapareció solo. **Esa es la señal a buscar en el resto:** un servicio que
importa `app.api.v1.<algo>` no es un servicio con una dependencia rara, es lógica
de negocio que quedó del lado equivocado.

El reparto de las 3 805 líneas:

| Qué | Líneas | Cómo se portó |
|---|---|---|
| `periodos`, `anual`, `xm_api` | ~830 | verbatim — no tocaban la sesión |
| `consultas`, `piscinas` | ~340 | reescritas al ORM |
| 24 endpoints | ~2 400 | pasaron a funciones de servicio; la vista solo valida y responde |

Se movieron con un script (`portar()`, sustituciones explícitas sobre el AST) en
vez de a mano: en 2 400 líneas de aritmética financiera, transcribir es garantizar
una errata que ninguna prueba de rutas detecta.

**`api/v1/cumplimiento/parametros.py` no es un accesorio.** FastAPI valida el query
string contra la firma (`year: int = Query(..., ge=2020, le=2050)`) y devuelve 422;
DRF no valida query params. Sin ese módulo, `?year=1999` llegaría a la consulta y
devolvería `{"contratos": []}` con 200 — un mes vacío en vez de un error.

## Los 47 recursos están portados

Al 2026-09-04: **47 de 47 recursos, 493 de 493 endpoints**. `tests/test_paridad_urls.py`
compara los dos árboles enteros y ya no hay ninguna ruta de FastAPI que Django no
sirva.

Los dos últimos fueron los caros por razones distintas:

- **`panel_contable`** (18 endpoints, 2 086 líneas): el más grande. Su servicio
  quedó partido en ocho módulos bajo `apps/contabilidad/services/`
  (`er_loader`, `desde_api`, `impuestos`, `costos`, `er_diario`, `er_export`,
  `reparto`, `panel`), porque el archivo original mezclaba el parseo del Excel,
  el reparto por inversionista, los impuestos y la serialización de la respuesta.
- **`reporte_cgm`** (1 endpoint): una línea de URL, 850 de servicio. Se separó en
  `apps/energia/services/cgm.py` —Quoia y openpyxl, sin base— y
  `cgm_envio.py` —quién recibe qué, que es lo único que consulta la base—.
  `resolver_borders` no se duplicó: ya vivía en
  `apps/energia/services/reporte/borders.py`, compartido con el reporte de energía
  para que ambos lean el MISMO catálogo.

**`/health` no cuelga de `/api/v1`** y por eso se escapaba del recorrido: el
walker de la paridad solo leía el mapa de acciones de los `ViewSet`, así que una
vista de función quedaba invisible. Ahora la vista declara sus verbos en
`metodos_http` y el walker los lee — sin eso, la última ruta faltante aparecía
como "portada" simplemente porque el test no sabía mirarla.

**Escotillas a SQLAlchemy bajo `apps/`: ninguna.** La última,
`apps/liquidaciones/services/excel.py` (el cargador de Excel de liquidaciones),
se retiró el 2026-10-05 junto con su endpoint: nadie lo usaba desde julio. La de
`balance_energia` se cerró al portar `cumplimiento`. Los clientes HTTP, SMTP e
IMAP se portaron a `apps/`; lo que el árbol Django todavía toma de `app/` está en
`PRESTADOS_DE_APP` (`tests/test_solo_django.py`).

**Lo que sigue no es portar, es apagar.** FastAPI y Django sirven hoy las mismas
493 rutas; el corte es una decisión de despliegue (a qué proceso apunta el
frontend), no de código. Antes de retirar `app/` faltan dos cosas que esta
migración dejó anotadas y no resolvió: la dueña de las tablas de `django_tracker`
y las 18 tablas muertas.

## Qué enseñaron los tres primeros recursos

Se portaron `retos`, `polizas` y `starlink` a mano antes de decidir si valía la
pena un comando que generara el esqueleto de un recurso. La respuesta es que no,
y el motivo es que los tres no se parecen:

| | `retos` | `polizas` | `starlink` |
|---|---|---|---|
| Clave de la ruta | id del recurso | id de **otro** modelo (`proyecto_id`) | el período, `YYYY-MM` |
| Forma | CRUD + acciones | listado + upsert | 8 acciones, ninguna CRUD |
| `queryset.py` | consultas + armado | join de 3 tablas, aplanado | resolución por lote |
| Serializers | `ModelSerializer` | campos a medida sobre 3 modelos | ninguno: dicts |
| Persiste | sí | sí | 3 de 8 endpoints no tocan la base |
| Paginación | envoltura propia | sin paginar | sin paginar |

Un generador tendría que haber acertado la respuesta a las seis filas el primer
día. Lo único genuinamente repetido es el `urls.py` —cuatro líneas de router— y
copiar cuatro líneas no necesita herramienta.

Lo que sí se repite y por eso está en la capa compartida: la autenticación, los
permisos, la paginación, los decoradores de log y los campos de `api/fields.py`.

## Lo que NO se copió de Origina, y por qué

| De Origina | Acá | Motivo |
|---|---|---|
| Apps planas en la raíz | `apps/<dominio>/` | 115 tablas; la raíz ya tiene 10 directorios |
| `settings.py` de 1022 líneas | `config/settings.py` | El de Origina no está partido por entorno y su propia sesión desaconsejó copiarlo |
| `GRPCAuthenticationMiddleware` | `api/authentication.py` | Este backend firma sus propios JWT (HS256, `SECRET_KEY`). Los tokens vigentes siguen siendo válidos |
| `FileValueField`, `FileWithStatusField` | — | Resuelven URLs contra Backblaze B2; acá el almacenamiento es S3 |
| `SupplyContextMixin` | — | Importa `services.grpc_request` e `investment.models`. El patrón sirve, la clase no |
| `log_endpoint` que traga la excepción | Re-lanza | Cambiaría el contrato de error de 493 endpoints |
| `Timer` repetido por app | Uno en `apps.plataforma` | Son las mismas dos columnas en 115 tablas |
| Un comando `startresource` | — | Los tres primeros recursos portados acá no comparten forma (ver abajo). Lo invariante son ~10 líneas de registro de router, que no valen un generador |
| `cachalot` | Apagado | Ver «Decisiones tomadas» |
| `easyaudit` junto a `django_tracker` | — | Origina corre los dos con uno casi apagado. Un sistema de auditoría, no dos |
