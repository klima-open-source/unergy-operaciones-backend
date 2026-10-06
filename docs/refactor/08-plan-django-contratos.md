# 08 · Contratos, proyectos y servicios en Django — plan de la rama `contratos-tabla-unica`

**Qué es esto:** el plan de la Fase 6 de `06-plan-migracion.md` (la fusión de contratos), reescrito
para el backend Django y recortado al alcance que decidió Sara el 2026-10-06. Reemplaza a `06` **solo
para esta parte**; el resto de `06` sigue escrito para FastAPI/Alembic y no aplica tal cual.

**Rama:** `contratos-tabla-unica`, sale de `worktree-modelado-tarifas` (Camilo, 17 commits del 24 al
28-sep) con `master` ya integrado (2026-10-06).

**Números de referencia** (copia local de producción al 2026-10-01; se vuelven a medir en producción
antes del deploy 1): 35 PPA (`ppa_contratos`, ids 1–36: 13 de compra, 22 de venta) · 160 contratos de
servicio (`contratos_servicio`, ids 1–221: 94 representación, 31 arriendo, 31 mantenimiento, 4
internet) · 43 vínculos PPA–planta (`ppa_contrato_proyectos`).

---

## 1 · Qué cambia de `06` al pasar a Django

| `06` decía | En Django |
|---|---|
| Revisión de Alembic | Migración en `apps/<app>/migrations/`. La aplica el servicio `migrate` del compose en cada deploy |
| Backfill como tarea `*_seed` en `_deferred_init` | Comando de `manage.py`, corrido **a mano** en el servidor (`docker compose exec operaciones python manage.py …`) y borrado después. Nunca dentro de una migración (`CLAUDE.md`), salvo la excepción del §5 |
| Railway, auto-deploy de `master` | Push a `master` = deploy. `scripts/verificar_esquema_django.py` **detiene el deploy** si un modelo no cuadra con la base |
| Borrar una tabla vieja | `SeparateDatabaseAndState`: Django deja de conocerla en el deploy 2 **sin borrarla**; el `DROP` va en el deploy 3 |
| Tests en SQLite, sin forma de probar lo específico de Postgres | Se ensaya en la copia local (`pg17`), con datos reales y sin tocar producción |

**Consecuencia que gobierna el plan:** una migración en `master` se ejecuta en el siguiente deploy. Por
eso **cada deploy es su propio PR**, y se junta a `master` cuando se quiere ejecutar.

## 2 · Alcance

**Entra:** contratos, su vínculo con proyectos, los servicios de cada contrato, y retirar las banderas
`srv_*` de `proyectos`.

**Queda para otras ramas** (decisión de Sara, 2026-10-06):

| Rama siguiente | Qué trae |
|---|---|
| Inversionistas | `contrato_proyectos` pasa a apuntar a `proyecto_inversionistas` (diagrama de Sara); el proyecto de los contratos de servicio deja su campo directo y entra a `contrato_proyectos` (hoy se lee así en ~20 archivos) |
| Tarifas | Plantillas (`servicio_plantillas`, `tarifa_plantillas`) y `tarifas` colgando de `servicios`. Es el modelo del commit `3dd6bb50`, que Camilo revirtió |
| Clientes | `contrato_partes`, ya con quién la escribe y quién la lee |

## 3 · El modelo al terminar

| Tabla | Qué es | Origen |
|---|---|---|
| `contratos` **(nueva)** | Todos los contratos en una tabla, con `grupo` explícito (`ppa`, `representacion_cgm`, `operacion`). Tabla ancha: lleva las columnas de las dos tablas viejas. **Es transitoria** hasta las ramas de tarifas y clientes | Camilo, + `grupo` |
| `contrato_proyectos` **(nueva)** | Contrato ↔ proyecto de los PPA. Reemplaza a `ppa_contrato_proyectos` | Camilo |
| `servicios` **(nueva)** | Una fila por contrato y por servicio que cubre. Valores = catálogo de `apps/contratos/services/grupos.py`: `compra`, `venta`, `representacion`, `cgm`, `mantenimiento`, `arriendo`, `internet`. Cuelga del **contrato** (no de `contrato_proyectos`: un contrato de servicio tiene un solo proyecto y la compra/venta de un PPA es del contrato entero) | `contrato_tipos` de Camilo, renombrada y con el catálogo de Sara |
| `ppa_contratos`, `contratos_servicio`, `ppa_contrato_proyectos` | Se dejan de usar en el deploy 2 y se borran en el deploy 3 | — |
| Satélites | Quedan igual; su FK pasa a `contratos` | — |

**Fachadas:** `PpaContrato` y `ContratoServicio` pasan a ser modelos *proxy* sobre `contratos`, filtrados
por `grupo` (hoy la rama filtra por `servicio_aplica` vacío o no: se cambia). El resto del código los
sigue usando igual.

**Se sacan de la rama de Camilo:** `contrato_partes`, `contrato_tarifas`, `contrato_tipos` (esta última
se convierte en `servicios`). Motivo: nadie las lee ni las escribe; llenadas una sola vez por el
backfill, se desactualizan con la primera edición. Es la lección de las banderas `srv_*` y de la
revisión 118 de Alembic: *una tabla nueva entra con su escritura y su lectura, o no entra.*

**Condición para `servicios`:** en esta misma rama,
- la escriben los formularios de servicios y de PPA, y la firma del CRM;
- la leen `subservicios_de()` y `subservicio_de_ppa()` (`grupos.py`), que dejan de deducir el servicio
  por las tarifas cargadas. Conservan nombre y salida: sus 4 llamadores no cambian.

## 4 · Decisiones

| # | Decisión | Estado |
|---|---|---|
| 1 | Los PPA conservan su `id` en `contratos` (paso 6.1 de `06`, Juanjo 2026-08-24) | **Confirmada** por Sara 2026-10-06. Razones vigentes: 8 tablas cuelgan del id del PPA y así no se tocan, entre ellas las de plata (`ppa_tarifas`, `cumplimiento_mensual`, `clasificacion_energia_mensual`, `asic_solicitudes`, `ppa_compromisos_energia`; las otras tres son `alertas`, `oportunidad_ofertas`, `cliente_documentos_comerciales`); Cumplimiento guarda meses cerrados con ids de PPA en el `localStorage` de cada navegador (`CumplimientoV2View.vue:5520`) |
| 2 | Los contratos de servicio reciben id nuevo, por encima del mayor existente; se reescriben sus 9 referencias | Consecuencia de 1 (los ids 1–36 chocan) |
| 3 | La reescritura de esas 9 referencias va **dentro de la migración del corte**, con una tabla de correspondencia `viejo → nuevo` | **Confirmada** por Sara 2026-10-06. Es una excepción a la regla de `CLAUDE.md`, a propósito: cambiar de tabla y reescribir los ids tiene que ser el mismo instante (antes, la app vieja no encuentra el contrato; después, la nueva lo cruza con otro), y dentro de la migración es todo o nada: si falla, Postgres deshace y el deploy se detiene. Descartado: apagar la app y correr un comando a mano entre dos migraciones |
| 4 | Los PPA también tienen filas en `servicios` (`compra` o `venta`) | **Confirmada** por Sara 2026-10-06. `tipo_contrato` y la fila de `servicios` se escriben desde un solo lugar del código, nunca por separado |
| 5 | Retirar `srv_*` en esta rama, después del barrido de proyectos | **Confirmada** por Sara 2026-10-06 |
| 6 | Contratos 62 y 69 (`tarifa_cgm = 0` con representación real) | Sin efecto en esta rama: las tarifas no se mueven |

## 5 · Despliegues

### Antes del deploy 1 (sin deploy)

- **Captura de referencia en producción, solo lectura:** números de Cumplimiento por período,
  `ppa.id` que devuelve `GET /comercial/proyectos-operando`, conteos de las tres tablas viejas, una
  muestra de `/liquidaciones`. Se guarda fuera del repo.
- **Ensayo completo en `pg17`:** deploy 1 → copia → deploy 2 → verificaciones del §6.

### Deploy 1 — tablas nuevas, vacías

- Migración: crea `contratos`, `contrato_proyectos`, `servicios`. Nada más.
- El código no cambia: nadie lee las tablas nuevas.
- **Rollback:** revertir el PR (la migración inversa borra tablas vacías).

### Copia — a mano, en el servidor

```bash
docker compose exec operaciones python manage.py backfill_contratos_unificados --dry-run
docker compose exec operaciones python manage.py backfill_contratos_unificados
```

- Copia los PPA **con su id**, los contratos de servicio con id nuevo, `ppa_contrato_proyectos` →
  `contrato_proyectos`, y genera `servicios` con la regla de hoy (`subservicios_de()` /
  `subservicio_de_ppa()`). Deja la tabla de correspondencia de ids de servicio.
- Las tablas viejas siguen siendo las que usa la app.
- **Pausa de edición de contratos** desde la copia hasta el deploy 2 (se avisa al equipo). La copia se
  repite (`--reset`) justo antes del deploy 2.
- Se revisa el reporte del `--dry-run` antes de la copia real.

### Deploy 2 — el corte

- Las FK de los satélites pasan a `contratos`. Las 8 de PPA no cambian de valor; las 9 de servicio se
  reescriben con la tabla de correspondencia (decisión 3).
- `PpaContrato`, `PpaContratoProyecto` y `ContratoServicio` pasan a ser fachadas.
- Las tablas viejas salen del estado de Django con `SeparateDatabaseAndState`: **siguen en la base, con
  sus datos**.
- Se ajusta el SQL directo contra las tablas viejas (`apps/clientes/services/gestion.py:142` y `:155`).
- `servicios` queda conectada (§3).
- **Rollback:** revertir el PR. La migración inversa vuelve a apuntar las FK a las tablas viejas y
  deshace la reescritura con la misma tabla de correspondencia. Hay que escribirla y ensayarla en `pg17`
  antes, no improvisarla.

### Barrido de servicios y retiro de banderas

1. Con la pantalla nueva, el equipo revisa qué servicios cubre cada contrato.
2. `manage.py revisar_servicios_banderas` tiene que dar **cero diferencias**.
3. PR que cambia los lugares que leen `srv_*` (hoy 47 líneas en 19 archivos del backend y 6 del front;
   entre ellos el sondeo MGS, las alarmas y el informe FMO) por una regla única: *la planta tiene el
   servicio si tiene un contrato vigente con ese servicio en `servicios`* (vigencia según
   `apps/contratos/services/vigencia.py`).
4. Otra migración borra las columnas `srv_*`, y el front quita sus casillas.

**No se aplica en producción antes del paso 2:** una planta con bandera y sin contrato cargado saldría
del monitoreo sin aviso (eran ~36 en septiembre).

### Deploy 3 — limpieza, al menos dos semanas después del deploy 2

- `pg_dump` de `ppa_contratos`, `contratos_servicio` y `ppa_contrato_proyectos` a un archivo fuera de la
  base.
- Migración con el `DROP` de las tres.
- Se borra `backfill_contratos_unificados`.
- **Rollback:** restaurar del dump. Es el único paso sin vuelta atrás por deploy.

## 6 · Verificación (en `pg17` antes de cada deploy, y en producción después)

- `contratos` tiene 35 + 160 filas (o lo que diga la captura); los 35 ids de PPA son idénticos a los
  de `ppa_contratos`.
- Cumplimiento da **los mismos números** que en la captura, mes por mes.
- `GET /comercial/proyectos-operando` devuelve los mismos `ppa.id` para los mismos contratos.
- `/liquidaciones`, Servicios (las tres pestañas), detalle de PPA y de contrato abren sin error.
- Cada contrato tiene al menos una fila en `servicios`, y `subservicios_de()` da lo mismo que antes del
  corte para todos los contratos.
- Las pruebas golden (`test_facturacion_golden_django.py`, `test_costos_golden_django.py`) pasan.

## 7 · Pasos de trabajo en la rama

1. Este plan.
2. Ajustar el modelo: sacar `contrato_partes` y `contrato_tarifas`; `contrato_tipos` → `servicios` con el
   catálogo de `grupos.py`; agregar `grupo`; la copia conserva los ids de PPA.
3. Partir las migraciones en deploy 1 / deploy 2 / deploy 3.
4. Conectar `servicios` (formularios, CRM, `subservicios_de()`, `subservicio_de_ppa()`).
5. Ensayo completo en `pg17`.
6. PR por deploy.

## 8 · Lo que se puede hacer ya, en paralelo

El barrido de **existencia y vigencia** de contratos (plantas con bandera y sin contrato: 74 en
septiembre): se carga en las pantallas de hoy y la copia lo pasa a `contratos`. **No** se revisa todavía
qué servicios cubre cada contrato: hoy eso se expresa cargando tarifas, y se haría dos veces.
