# Diagnóstico del PPA: cómo se crea hoy, qué tablas lo sostienen y qué está roto

Escrito para el equipo de desarrollo. Fecha del corte: **2026-09-17**. Todas las
cifras de producción salen de consultas de solo lectura corridas ese día contra
la base `operations`.

Este documento es **descriptivo**: dice cómo es hoy. El modelo objetivo del CRM
está en [`DOMINIO_COMERCIAL.md`](DOMINIO_COMERCIAL.md) y no se repite acá; donde
los dos se tocan, se cita.

---

## Resumen: los siete hallazgos

| # | Hallazgo | Gravedad | Estado |
|---|---|---|---|
| 1 | `POST`/`PATCH /ppa` **descartaba en silencio** `comprador_id`, `vendedor_id` y `responsable_id`. El front manda esas tres claves; el backend esperaba `comprador`, `vendedor`, `responsable`. Regresión del port a Django (FastAPI sí las aceptaba). | 🔴 Alta | ✅ `aecee09a` |
| 2 | `POST /ppa/{id}/proyectos` **no existe** en el backend —nunca existió, tampoco en FastAPI— y el botón «asociar planta» del detalle daba 404. Se resolvió por el `PATCH`, sin agregar endpoint. | 🔴 Alta | ✅ `2ebe36cf` |
| 3 | El camino «firmar una oferta» (`POST /comercial/ofertas/{id}/firmar`) **no se ha usado nunca**: 0 de 165 ofertas tienen `ppa_contrato_id`. Los 35 PPA se crearon por el otro camino. | 🟠 Media | abierto |
| 4 | `firmar()` y `POST /ppa` **aplicaban reglas distintas** sobre la misma tabla. Ahora los dos llaman a `escritura.crear_ppa()`, que tiene las reglas una sola vez y exige el tipo de contrato en vez de adivinarlo. | 🟠 Media | ✅ Fase 1 |
| 5 | `ppa_contratos` guarda una **copia a mano** de la información de GESCON en seis columnas (`gescon_*`, `codigo_sic`) que ninguna lógica del backend lee, que casi nadie llena, y que ya divergieron de la fuente real. | 🟠 Media | abierto |
| 6 | Datos incompletos: de los PPA vivos, **11 no tienen planta vinculada**, **13 no tienen tarifas** y **14 no tienen compromisos de energía**. Sin compromisos, Cumplimiento no puede medir el contrato. | 🟠 Media | abierto |
| 7 | **No había ni una prueba** que ejerciera el cuerpo de una escritura de `/ppa`. Por eso el hallazgo 1 pasó el deploy. Hoy hay 31, en `tests/test_ppa_escritura_cuerpo.py`. | 🟡 Baja | ✅ |

---

## 1 · El mapa de tablas

### Las seis tablas del dominio `ppa`

| Tabla | Modelo | Qué guarda | Filas (2026-09-17) |
|---|---|---|---|
| `ppa_contratos` | `PpaContrato` | El contrato: partes, fechas, tarifa base, indexación, cantidades, datos GESCON, tipo. Borrado **lógico** (`deleted_at`). | 35 (33 vivos) |
| `ppa_contrato_proyectos` | `PpaContratoProyecto` | N↔N contrato ↔ planta. PK compuesta, sin `id`. | 42 |
| `ppa_tarifas` | `PpaTarifa` | Precio **por (año, mes)**. Único `(contrato, año, mes)`. | 2 433 |
| `ppa_compromisos_energia` | `PpaCompromisoEnergia` | Mínimo y máximo de energía **por (año, mes)**, en MWh. | 2 432 |
| `ppa_responsables` | `PpaResponsable` | Catálogo de quién responde por el contrato, con la bandera `incluir_en_cumplimiento`. | 2 (`Unergy`, `Externo`) |
| `ipp_mensual` | `IppMensual` | El IPP por mes. No cuelga de ningún contrato: es un índice global que la facturación usa para indexar. | — |

Detalle de las series mensuales: `tarifa` no es nula en ninguna de las 2 433
filas, y `energia_minima` no es nula en ninguna de las 2 432. Cuando hay datos,
están completos; el problema es **cuáles contratos no tienen ninguna fila**.

### Quién apunta a `ppa_contratos`

Seis tablas de otros dominios tienen FK al contrato. El `on_delete` importa,
porque el borrado real solo ocurriría por consola:

| Tabla origen | Columna | `on_delete` | Para qué |
|---|---|---|---|
| `asic_solicitudes` | `contrato_ppa_id` | `DO_NOTHING` | El registro GESCON/ASIC ante XM. **Es el que conecta el PPA con el despacho real.** |
| `cumplimiento_mensual` | `contrato_ppa_id` | `CASCADE` | El cierre mensual de cumplimiento. **Hoy está vacía: 0 filas.** |
| `clasificacion_energia_mensual` | `contrato_ppa_id` | `SET_NULL` | Clasificación mensual de la energía. |
| `alertas` | `ppa_id` | `CASCADE` | Alertas de vencimiento (90/60/30 días). Único `(ppa_id, days_to_expiration)`. |
| `cliente_documentos_comerciales` | `ppa_contrato_id` | `CASCADE` | El enlace de Drive del contrato vive acá, como documento `tipo='contrato'`. 29 filas apuntan a un PPA. |
| `oportunidad_ofertas` | `ppa_contrato_id` | `SET_NULL` | El enlace con el CRM. **0 filas lo tienen puesto.** |

Y `ppa_contratos` apunta hacia afuera a `clientes` (dos veces: comprador y
vendedor) y a `ppa_responsables`.

### El dato que no está en ninguna tabla: el estado

`ppa_contratos` **no tiene columna `estado`**. Lo que la UI muestra son dos
derivados distintos y no hay que confundirlos:

- **Vigencia** (`vigente` / `por_vencer` / `vencido` / `por_iniciar`) — se
  calcula de las fechas. El front la deriva en
  `app/features/contratos/utils/ppaVigencia.ts`, porque el listado `GET /ppa` no
  trae `dias_restantes` y el detalle sí. Está documentado y es deliberado.
- **Cumplimiento** (`estado_cumplimiento`, `cobertura_actual_pct`) — mide
  generación contra el compromiso mínimo del mes
  ([`contratos.visibilidad`](../apps/ppa/services/contratos.py)). Viaja **solo en
  el detalle y en el resumen global**, nunca en el listado: son dos consultas por
  contrato, y en una lista de 500 serían mil.

---

## 2 · Los dos caminos de creación

### Camino A — `POST /ppa` (el que se usa)

`PPAContratoWizard.vue` → `POST /api/v1/ppa` →
[`api/v1/ppa/views.py:97`](../api/v1/ppa/views.py#L97)

En una transacción: crea la fila, valida `fecha_fin` contra los registros GESCON
de sus plantas (422 si algún registro termina después), fija los proyectos,
sincroniza nombre y NIT desde el cliente, y guarda el enlace de Drive como
documento comercial. **No crea tarifas ni compromisos**: el wizard los manda
después, con `PUT /ppa/{id}/tarifas` y `PUT /ppa/{id}/compromisos`.

Son tres peticiones seguidas sin transacción común. Si la segunda falla, queda un
contrato sin tarifas y el usuario no tiene cómo saberlo salvo mirando el detalle.

### Camino B — `POST /comercial/ofertas/{id}/firmar` (el que no se usa)

`FirmarOfertaDialog.vue` → [`escritura.firmar()`](../apps/comercial/services/escritura.py#L358)

Crea el PPA desde la oferta aceptada, vincula **todas** las plantas de la oferta,
expande la tabla de precios anuales a filas mensuales de `ppa_tarifas` recortadas
al período de suministro, y mueve la oferta a `firmado` con su historial.

**0 de 165 ofertas tienen `ppa_contrato_id`.** El camino está construido y
probado, pero ningún PPA de producción nació por ahí.

### Lo que hacía uno y el otro no — resuelto en la Fase 1

Así estaban los dos caminos antes del 2026-09-17. Se deja la tabla porque explica
de dónde salen los datos torcidos que hay en producción:

| | `POST /ppa` | `firmar()` |
|---|---|---|
| Valida `fecha_fin` contra GESCON | ✅ | ❌ |
| `sincronizar_partes` (nombre/NIT desde el cliente) | ✅ | ❌ (copiaba el vendedor a mano) |
| Escribe `comprador_id` / `responsable_id` | ✅ | ❌ nunca |
| Elige `tipo_contrato` | ✅ del payload | ❌ fijo en `"compra"` |
| Crea `ppa_tarifas` | ❌ (petición aparte) | ✅ en la misma transacción |
| Crea `ppa_compromisos_energia` | ❌ (petición aparte) | ❌ nunca |
| Enlaza la oferta | ❌ | ✅ |
| Todo en una transacción | ✅ el contrato; no las series | ✅ completo |

**Hoy las reglas viven en `apps/ppa/services/escritura.py::crear_ppa()`** y los dos
caminos la llaman. Lo que queda propio de cada uno es de dónde salen los datos:
el wizard los recibe del formulario, `firmar()` los saca de la oferta y además
enlaza y mueve de estado.

Tres cosas que la función fija, y que antes dependían del camino:

- **El tipo de contrato es un parámetro obligatorio.** `TIPO_PPA_POR_OFERTA` mapea
  el tipo de oferta al de contrato y revienta con uno desconocido. Un default
  silencioso grabaría al cliente del lado equivocado el día que exista la oferta
  de venta, y eso llega hasta la facturación sin fallar de forma visible — es lo
  que `DOMINIO_COMERCIAL.md` llama «el punto más delicado de toda la etapa».
- **Un contrato sin plantas se crea, pero avisa.** El caso es legítimo (la planta
  puede no existir todavía como `Proyecto`), así que no se bloquea; el resultado
  trae `avisos` y la UI los muestra.
- **Todo o nada.** Contrato, plantas, tarifas y compromisos en una transacción.
  `POST /ppa` acepta las dos series en el mismo cuerpo y el wizard las manda
  juntas al crear. Al editar siguen yendo por su `PUT`, porque ése reemplaza el
  conjunto y mandarlo en cada edición borraría las series de quien solo vino a
  corregir una fecha.

Sigue abierto lo que no es del contrato sino del CRM: **ninguna oferta se ha
firmado nunca** (0 de 165), y `firmar()` todavía no escribe a Unergy como
comprador — la contraparte se pasa como dato, no está cableada.

---

### Y dos cosas que *parecen* un tercer camino, y no lo son

La UI dice «contrato» en cuatro lugares y son cuatro objetos distintos. Conviene
tenerlo claro antes de tocar nada:

| Pantalla | Qué crea | Dónde queda |
|---|---|---|
| Servicios → **PPA nuevo** | el contrato de energía | `ppa_contratos` |
| MEM → GESCON → **Registrar** | el registro ante XM | `asic_solicitudes` |
| Finanzas → **Crear contrato de energía** | un contrato del **servicio externo** de Liquidaciones | `/liquidaciones-api/contratos-energia`, otra base |
| Cumplimiento → **PPA nuevo** | **nada** | memoria del navegador |

El de Cumplimiento (`CumplimientoV2View.vue`, junto a *"arrastra las plantas
entre contratos para simular"*) construye un objeto en memoria con id de texto
`__ficticio_N` y bandera `_ficticio`. No hay `POST`. Se pierde al recargar. Pide
solo nombre, mínimo y máximo MWh porque es lo único que necesita el cálculo de
cobertura. Las únicas escrituras reales de esa pantalla son
`POST /ppa/responsables` y `POST /ppa/responsables/asignar`.

Vale un cambio de una palabra en la etiqueta —«PPA simulado»— para que nadie más
crea que ahí se crea un contrato.

## 3 · Qué consume el front y dónde no cuadra

`app/features/contratos/services/ppa.ts` declara 18 rutas. Estado de cada una:

| Ruta del front | Backend | Estado |
|---|---|---|
| `GET /ppa`, `GET /ppa/{id}`, `DELETE /ppa/{id}` | sí | ✅ |
| `POST /ppa`, `PATCH /ppa/{id}` | sí | ⚠️ **pierde 3 campos** (abajo) |
| `PUT /ppa/{id}/tarifas`, `PUT /ppa/{id}/compromisos` | sí | ✅ |
| `POST /ppa/{id}/proyectos` | **no existe** | 🔴 **404** |
| `GET|POST /ppa/responsables`, `PATCH|DELETE /ppa/responsables/{id}`, `POST /ppa/responsables/asignar` | sí | ✅ |
| `GET /cumplimiento/ppa/{id}/plantas-inscritas-por-mes` | sí | ✅ |
| Las 7 de `/asic` | sí | ✅ |

### Hallazgo 1, con la evidencia

El serializer de lectura devuelve `responsable_id`, `comprador_id`, `vendedor_id`.
El de **escritura** declara los campos como FK de Django, así que las claves que
acepta son `responsable`, `comprador`, `vendedor`. DRF descarta en silencio lo que
no reconoce. Ejecutado contra el serializer real:

```
payload del front:  {..., "responsable_id": 1, "comprador_id": 5, "vendedor_id": 7, ...}
valido: True
DESCARTADO: ['responsable_id', 'comprador_id', 'vendedor_id']
```

Devuelve **201**. El contrato se crea sin responsable, sin comprador y sin
vendedor; y como `sincronizar_partes()` solo actúa cuando hay un `*_id`, tampoco
copia nombre ni NIT. El usuario ve el contrato creado y no ve el hueco.

Que es una regresión del port está en el código apagado: `app/schemas/ppa.py:72`
(`PPAContratoCreate`) declaraba `comprador_id`, `vendedor_id` y `responsable_id`.
El front se escribió contra ese contrato y nunca se cambió. La corrección natural
es del lado del backend: aceptar las tres claves `*_id`, que es lo que la API
publicaba antes y lo que la lectura sigue devolviendo.

Efecto colateral del mismo desajuste: la edición de partes del detalle
(`guardarPartes`) manda solo nombre y NIT. En un contrato **con** `comprador_id`,
`sincronizar_partes()` corre después de guardar y **pisa** lo que el usuario
acaba de escribir con lo que dice la ficha del cliente. En uno sin FK —los que
crea el wizard hoy— la edición sobrevive. Mismo botón, dos comportamientos.

---

## 4 · Estado de los datos en producción

**35 contratos** (34 vivos, 1 borrado lógico). Vigencia: 24 vigentes, 9 vencidos.
Rango de fechas: 2023-11-23 → 2041-12-31.

### Cuándo se cargaron

| Fecha | Contratos |
|---|---|
| 2026-05-02 … 2026-05-22 (6 días) | 34 |
| 2026-09-17 | 1 |

La carga es de mayo, casi toda en tres días. El contrato **id 36**, creado el
2026-09-17 a las 10:40 de Bogotá, tiene **todos los campos en null salvo
`tipo_contrato='compra'`**: sin código, sin nombre, sin fechas, sin partes. Vale
la pena confirmar si es una prueba y borrarlo, o si es un intento de creación que
se quedó a medias.

### Qué tan llenos están los 34 vivos

| Campo | Con dato | Observación |
|---|---|---|
| `responsable_id` | 33 | 26 `Unergy`, 7 `Externo`, 1 sin responsable |
| `vendedor_id` | 24 | |
| `comprador_id` | **7** | El vínculo con el cliente comprador casi no existe |
| `comprador_nombre` | 33 | El texto sí está; la FK no |
| `vendedor_nombre` | 29 | |
| `tarifa_base` | 11 | Los otros 23 indexan por `ppa_tarifas` |
| `codigo_sic` | 10 | |
| `es_comunidad_energetica` | 0 | Nadie ha marcado uno |
| fechas completas | 33 | Falta el id 36 |

`tipo_contrato`: **21 venta, 13 compra** — ninguno nulo. Ojo con la asimetría que
esto revela: en los 13 de compra, 12 tienen "Unergy" en `comprador_nombre` pero
solo 1 tiene `comprador_id`; en los 21 de venta, 9 tienen "Unergy" en
`vendedor_nombre`. La identidad de Unergy como parte está escrita como texto, no
como relación.

### Las series y las plantas

| | Contratos vivos que lo tienen | Que **no** lo tienen |
|---|---|---|
| ≥1 planta vinculada | 23 | **11** |
| ≥1 fila en `ppa_tarifas` | 21 | **13** |
| ≥1 fila en `ppa_compromisos_energia` | 20 | **14** |

Los 11 sin planta son casi todos de la familia Terpel/NEU (ids 4–12) más dos de
compra (26, 32) y el id 36. Un PPA sin planta **no lo puede medir Cumplimiento**:
no hay generación contra qué comparar. El código de `firmar()` ya trata esto como
un caso legítimo pero visible (devuelve `plantas_del_contrato` para que la UI
avise); por `POST /ppa` no hay ningún aviso equivalente.

### Coherencia de las series con el período del contrato

- **294 filas de `ppa_tarifas` caen fuera** del rango `fecha_inicio`–`fecha_fin`
  de su contrato. No rompen nada —la facturación busca el mes que necesita— pero
  son precio declarado para meses en los que el contrato no está vigente.
- **15 meses del período no tienen tarifa**, en contratos que sí tienen serie
  cargada. Esos meses no se pueden facturar por tarifa indexada.

Vale la pena decidir si `PUT /ppa/{id}/tarifas` debe recortar al período, como ya
hace `firmar()` con `tarifas_mensuales()`.

### El vínculo con GESCON: sano. La copia de sus datos: no

**El enlace funciona.** De 34 contratos vivos, **19 tienen registros GESCON por
FK**, y son exactamente los mismos 19 que coinciden por texto
(`contrato_interno = numero_codigo_contrato`): ningún contrato depende solo del
emparejamiento por nombre.

De los 220 registros ASIC, 120 tienen `contrato_ppa_id`. De los 100 que no:
**ninguno corresponde a un PPA existente** —su `contrato_interno` no coincide con
ningún contrato— y 70 son compras **UNGC**, que `piscinas.py` documenta como
GESCON puro, fuera del módulo PPA. Están bien sin enlace.

Lo que sí sobra es la **copia**: `ppa_contratos` carga seis columnas con
información que pertenece a `asic_solicitudes`.

| Columna | Contratos vivos que la tienen (de 34) |
|---|---|
| `gescon_codigo` | 4 |
| `gescon_fecha_inicio` · `gescon_fecha_fin` | 2 |
| `gescon_precio` · `gescon_cantidades_kwh` | **0** |
| `codigo_sic` | 10 |

**Ninguna lógica del backend las lee.** Ni Cumplimiento, ni Facturación, ni las
piscinas: todas van a `asic_solicitudes`. Lo único que las toca es la sección
GESCON del detalle del front, que las muestra y deja editarlas a mano.

Y ya divergieron: el contrato **19** declara `codigo_sic = 88749` cuando su
registro ASIC real es **87553** — y 88749 es el código del contrato **15**.

El problema de fondo no es la sincronización, es la cardinalidad: **un PPA tiene
muchos registros GESCON** (el contrato 2 tiene 32, el 4 tiene 24, el 1 tiene 21).
Una columna escalar no puede representar eso. La copia no cabe, no solo está
desactualizada.

`cumplimiento_mensual` tiene **0 filas**: el cierre mensual nunca se ha corrido.
Por eso la primera regla de `razones_para_no_borrar()` hoy nunca se activa, y
cualquier PPA con registros GESCON se protege solo por la segunda.

---

## 5 · El ciclo de vida hoy, de punta a punta

```
1. NACIMIENTO
   Wizard PPA → POST /ppa                    ← los 35 de producción
   (alternativa construida y sin usar: firmar una oferta del CRM)

2. CARGA DE CONDICIONES        dos peticiones más, fuera de la transacción
   PUT /ppa/{id}/tarifas       precio por (año, mes)
   PUT /ppa/{id}/compromisos   mínimo y máximo de energía por (año, mes)

3. REGISTRO ANTE EL MERCADO    POST /asic
   El registro GESCON es lo que conecta el contrato con el despacho real de XM.
   Regla cruzada: la fecha_fin del PPA no puede ser anterior a la de sus
   registros, y al revés.

4. OPERACIÓN MENSUAL
   Cumplimiento    generación de las plantas  vs  ppa_compromisos_energia
   Facturación     kWh del despacho × (tarifa_base × IPP_mes / IPP_base)
                   el despacho llega al PPA a través del registro ASIC, no de
                   la planta: AsicSolicitud.contrato_ppa_id
   Clasificación   clasificacion_energia_mensual

5. VIGILANCIA
   Tarea Celery ppa.alertas_vencimiento, diaria a las 8:15 (Bogotá).
   Crea una alerta por cada umbral cruzado: 90, 60, 30 días.
   Idempotente por el único (ppa_id, days_to_expiration).

6. FIN
   La fecha_fin pasa. El contrato queda "vencido" (derivado, no hay columna).
   comercial.cerrar_contratos_vencidos archiva la oferta asociada — hoy no
   archiva ninguna, porque ninguna oferta está enlazada.
   DELETE /ppa/{id} es borrado LÓGICO, y da 409 si cuelgan liquidaciones de
   cumplimiento o registros GESCON.
```

---

## 6 · Qué arreglar, en orden

### Hecho — Fase 0 (2026-09-17)

1. ✅ **`POST`/`PATCH /ppa` vuelven a aceptar `comprador_id`, `vendedor_id` y
   `responsable_id`.** Declarados como `PrimaryKeyRelatedField` con `source=`, así
   que además un id inexistente da 400 en vez de pasar de largo. Commit
   `aecee09a`.
2. ✅ **El botón «asociar planta» del detalle vuelve a guardar**, por el `PATCH`
   que ya existía. No se construyó `POST /ppa/{id}/proyectos` a propósito: habría
   dos formas de fijar las plantas de un contrato. Commit `2ebe36cf` en el
   frontend, con `plantasDelContrato.ts` y sus pruebas.
3. ✅ **Pruebas de cuerpo para todas las escrituras de `/ppa`** —creación,
   edición, tarifas, compromisos, borrado y responsables— en
   `tests/test_ppa_escritura_cuerpo.py`. Una de ellas falla si la lectura y la
   escritura vuelven a nombrar distinto las tres relaciones.
4. ✅ **El contrato 36 borrado** (lógico). Era una fila vacía creada el
   2026-09-17. Quedan 33 vivos.

No hubo backfill que hacer: entre el port a Django y el arreglo solo se creó ese
contrato, así que la regresión no dejó datos históricos dañados. Lo que sí falta
—los 26 contratos sin `comprador_id`— es anterior al port y se trata abajo.

### Hecho — Fase 1 (2026-09-17)

5. ✅ **Una sola función de escritura.** `apps/ppa/services/escritura.py::crear_ppa()`
   tiene las reglas una vez; `POST /ppa` y `firmar()` la llaman. El contrato se
   crea completo —plantas, tarifas y compromisos— en una transacción, o no se
   crea. El tipo de contrato es parámetro obligatorio con guarda. La respuesta
   trae `avisos` de lo que quedó cojo.
6. ✅ **`firmar()` cubierto por pruebas.** No tenía ninguna: las que existían
   (`test_comercial_pipeline_oferta.py`, `test_comercial_ficha_operativa.py`)
   ejercen el árbol FastAPI apagado. Once pruebas nuevas en
   `tests/test_comercial_firmar_ppa.py`, incluidas las dos reglas que este camino
   ahora hereda.
7. ✅ **`apps/comercial/services/documentos.py` eliminado.** Era un duplicado
   exacto de `apps.clientes.services.documentos.set_enlace` y, al mover la
   escritura del enlace a `crear_ppa`, se quedó sin un solo llamador.

### Pendiente, por orden de valor

8. **Partir `firmar()`: el CRM enlaza, no crea.** *(decidido por Sara el
   2026-09-17; pendiente de construir)*

   El diálogo de firma deja de pedir las condiciones del contrato. El comercial
   marca la oferta como firmada y **elige** el PPA —de una lista, o creándolo en
   Contratos y volviendo—; `firmar()` se queda solo con lo que es del CRM:
   enlazar `oferta.ppa_contrato_id`, mover a «firmado» y dejar el historial.

   Por qué: nunca se ha usado —**0 de 165 ofertas**— y pide exactamente las
   mismas condiciones que el wizard, con menos alcance (no crea compromisos, no
   escribe comprador ni responsable). La duplicación que queda no está en las
   reglas, que ya se unificaron en la Fase 1, sino en que hay **dos puertas para
   crear un contrato** y una no la usa nadie.

   Por qué NO se borra entero: es lo único que escribe el enlace entre la oferta
   y su PPA, y de ese enlace dependen `cerrar_contratos_vencidos` y la etapa
   «Operando» del modelo objetivo. Sin él, el pipeline seguiría adivinando el
   contrato de cada oferta emparejando por planta —el camino implícito que
   `pipeline.py` documenta como necesario justamente porque el enlace está vacío.

   Coincide con la regla del empalme: *«Desde Firmado la verdad vive en el
   CONTRATO. El CRM sólo LEE»* (`DOMINIO_COMERCIAL.md`, Etapa 3).

9. **Retirar la copia de GESCON.** El detalle deja de mostrar las seis columnas
   copiadas y muestra los registros ASIC reales del contrato, derivados de la
   relación que ya existe; se siguen editando donde se editan hoy, en GESCON.
   Sin nadie leyéndolas, las columnas salen con una migración. Es lo que más
   «una sola fuente de verdad» compra por lo poco que cuesta: hoy nada del
   backend depende de ellas.
10. **Las partes por llave, no por texto.** 26 de 33 contratos vivos no tienen
   `comprador_id`; el nombre está escrito a mano y casi ninguno resuelve
   automáticamente contra `clientes` (difieren el punto final, el formato del
   NIT). Es limpieza con criterio humano, no un script. Ojo con un supuesto que
   NO se sostiene: **«Unergy» no es un cliente canónico** — hay tres filas
   (`UNERGY S.A.S`, `UNERGY ENERGIA DIGITAL S.A.S E.S.P`, `Operaciones Unergy`) y
   los contratos usan dos razones sociales distintas como contraparte. Hay que
   decidir cuál aplica antes de enlazar nada.
11. **Higiene del resto de los datos**: los 11 sin planta, los 14 sin compromisos,
   las 294 tarifas fuera de período. Cada uno es una decisión de negocio, no un
   bug: hay que preguntarle a quien los cargó.
12. **Los nombres.** «PPA simulado» en Cumplimiento, y distinguir en Finanzas que
   ese «contrato de energía» es del servicio externo. Media hora, y elimina la
   confusión de §2-bis.

---

## Cómo reproducir las cifras

Las consultas están en los scripts del diagnóstico; todas son `SELECT`. Para
volver a correrlas contra producción:

```bash
uv run python manage.py shell -c "exec(open('ruta/al/script.py', encoding='utf-8').read())"
```

Ojo con el `encoding='utf-8'`: las columnas `año` de `ppa_tarifas` y
`ppa_compromisos_energia` llevan eñe, y en Windows `open()` sin encoding las
rompe.
