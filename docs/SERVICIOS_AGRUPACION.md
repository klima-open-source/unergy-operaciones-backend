# Servicios: agrupación en tres grupos y sus subservicios

Ruta de implementación y el razonamiento detrás. Creado el 2026-09-16.

**Estado: bloque A implementado hasta A3** (ver §9). Lo demás está sin escribir,
y los bloques B en adelante están **bloqueados**: necesitan la base de datos, y
el acceso desde local está caído por la allowlist de IP (§8).

Nada de lo hecho está commiteado ni desplegado, y **no se ha escrito un solo
dato de producción**.

## 1. Qué se quiere

Agrupar los servicios en **tres grupos**, cada uno con sus subservicios:

| Grupo | Subservicios | ¿Cuántos contratos? |
|---|---|---|
| **PPA** | compra, venta | **Uno**, y es de compra *o* de venta |
| **Representación y CGM** | representación, cgm | **Uno para los dos** (caso general), o uno por cada uno, o solo uno de los dos |
| **Operación** | mantenimiento, arriendo, internet | **Uno por subservicio** |

La regla que unifica los tres casos:

> **Un contrato cubre uno o más subservicios, todos del mismo grupo.**

Ese "uno o más" es **siempre uno**, salvo en Representación y CGM. En detalle:

- **PPA** — un contrato, de compra *o* de venta. Nunca los dos.
- **Operación** — **cada subservicio tiene su propio contrato**, siempre. El
  mantenimiento va en un contrato, el arriendo en otro y el internet en un
  tercero. **Nunca hay un contrato que cubra dos subservicios de Operación.**
  Una planta con los tres servicios tiene **tres contratos distintos**.
- **Representación y CGM** — el único caso con varias formas: un contrato para
  los dos subservicios (el caso general), un contrato por cada uno, o solo uno
  de los dos contratado.

Esa irregularidad de Representación y CGM es la única, y es la que hoy el
esquema no sabe expresar.

**Consecuencia para el diseño:** los grupos no son todos iguales por dentro.
En **Operación**, el grupo es una **colección de contratos independientes**, uno
por subservicio — agrupar es juntar para mostrar, nada más. En **Representación
y CGM**, un solo contrato puede *ser* el grupo entero. Por eso `subservicios`
tiene que ser una lista aunque casi siempre traiga un elemento: no es para
Operación, es para el caso combinado de Representación y CGM.

## 2. De dónde viene lo que hay hoy

Historia reconstruida del historial de Alembic (congelado en la revisión 143,
se conserva como bitácora).

| Fecha | Revisión | Qué pasó |
|---|---|---|
| 2026-05-25 | 011 `operacion_tabs` | Nacen `mantenimiento`, `arriendo`, `internet` en el enum `servicio_aplica`, junto con `pagos_servicio` |
| 2026-08-03 | 057 | Se agregan a `contratos_servicio` las columnas de internet (`plan_datos_gb`, `velocidad_mbps`, `tipo_conexion`) |
| 2026-08-27 | 118 | **Se borran las tablas `servicio_operacion` y `servicio_representacion`** |
| 2026-08-28 | 125 | Se elimina el valor `operacion` del enum |

Dos conclusiones que condicionan la ruta:

**(a) La agrupación es la intención original, no una idea nueva.** La migración
que creó los tres servicios de Operación se llama `operacion_tabs`, y llegó a
existir un valor `operacion` en el enum. Esto termina algo empezado en mayo.

**(b) El patrón "una tabla por servicio" ya se intentó y se revirtió.** La
revisión 118 dice por qué:

> *"Dos tablas de diseño temprano, nunca provisionadas vía Alembic, 0 filas en
> producción las dos… El reemplazo real y vigente de ambos conceptos es la tabla
> genérica `ContratoServicio`."*

Y sobre `servicio_representacion`, que **sí tenía vista en el front** pero
ningún endpoint de escritura:

> *"las columnas siempre mostraban '-', para siempre, porque no hay forma de
> cargar el dato."*

El valor `operacion` murió por lo mismo: **0 de 162 contratos** lo usaron nunca.

**La lección no es que el esquema estuviera mal, sino que se crearon estructuras
que nada llenaba.** Cualquier tabla nueva que se proponga acá tiene que venir
con su escritura y su UI, o repite el ciclo.

## 3. Qué existe hoy, exactamente

### Backend

- **`contratos_servicio`** — una fila por contrato. El subservicio vive en
  `servicio_aplica`, un enum de **un solo valor**: `representacion`, `cgm`,
  `mantenimiento`, `arriendo`, `internet`.
- **`ppa_contratos`** — tabla aparte. `tipo_contrato` (default `venta`) **ya es
  la subdivisión compra/venta**. Vínculo a plantas por `ppa_contrato_proyectos`
  (M2M: un PPA puede cubrir varias plantas; un contrato de servicio, una sola).
- **Detalle por subservicio, ya colgado del contrato** (FK detalle → contrato):
  - mantenimiento: `om_seleccion_mensual`, `om_documento_proyecto`, `om_factura_mensual`
  - arriendo: `arr_arrendador`, `arr_proyectos`, `arr_seleccion_mensual`, `arr_documento`
  - internet: **ninguna tabla** — vive en 13 columnas de `contratos_servicio`
- **Transversales**: `contrato_frontera`, `contrato_factura`, `pagos_servicio`.

### Frontend

Los tres grupos **ya existen como vistas**:

- `ServiciosUnificadoView.vue` (contenedor, 1838 líneas, en `LEGACY_PENDIENTE_DE_MIGRAR`)
- `PPAView.vue`, `RepresentacionView.vue`, `OperacionView.vue`
- `ContratoServicioWizard.vue` (escribe, incluidas las columnas de internet)
- `filtrosServicios.ts` — la agrupación y el filtrado son **JS plano en el front**

El backend no sabe nada de grupos: entrega una lista plana y el front la reparte.

## 4. El problema central

`filtrosServicios.ts` filtra así:

```ts
coincide(fila, 'servicio_aplica', f.tipo)
```

Igualdad exacta contra un campo de un solo valor. **Un contrato que cubre
representación y CGM a la vez no puede representarse**: hay que elegir uno, y el
otro queda invisible para todo filtro, todo conteo y todo panel.

Lo llamativo: **el esquema ya admite el caso, pero el enum no.** La fila tiene
las cuatro columnas juntas — `tarifa_representacion`, `tarifa_cgm`,
`indexacion_representacion`, `indexacion_cgm`. El dato cabe; la etiqueta no.

## 4-bis. Un PPA cubre varias plantas: qué cambia

Los contratos de Representación, CGM y Operación se asocian a **un único
proyecto** (`contratos_servicio.proyecto_id`). Un PPA puede cubrir **varias
plantas** (`ppa_contrato_proyectos`, M2M). Eso no es un detalle de forma: cambia
tres cosas.

### (a) Las cifras de un PPA son del contrato, no de la planta

Verificado en el esquema: `ppa_tarifas` y `ppa_compromisos_energia` cuelgan del
**contrato**, con unique en `(contrato, año, mes)`. `ppa_contrato_proyectos`
tiene **solo** `(contrato_id, proyecto_id)` — **no hay campo de reparto,
porcentaje ni participación por planta**.

O sea: **no existe el dato de cuánto de la tarifa o del compromiso le
corresponde a cada planta.** Se puede repartir por una regla (en partes iguales,
por potencia), pero es una regla inventada, no un dato.

Hay un indicio de que el reparto importa: `PpaCompromisoEnergia.cantidad_proyectos`,
que cumplimiento XM lee como `plantas_esperadas`
(`apps/mercado_xm/services/cumplimiento/`) — el número de plantas que se
esperaba que respaldaran ese compromiso.

**Consecuencia dura: sumar cifras de PPA por planta duplica.** Un PPA de 5
plantas mostrado en las 5 pestañas, con su tarifa completa en cada una, suma
cinco veces el mismo contrato. Cualquier total que mezcle servicios y PPA tiene
que contar el PPA **una sola vez**.

### (b) "Cuántos servicios tiene esta planta" y "cuántos contratos hay" dejan de
ser la misma pregunta

En el grupo Operación, un contrato es una planta, así que contar contratos o
contar plantas da casi lo mismo. En PPA no. Los conteos de cada grupo tienen que
decir explícitamente qué cuentan:

- **contratos** — un PPA de 5 plantas cuenta **1**
- **plantas cubiertas** — ese mismo PPA cuenta **5**

El código actual ya distingue `num_contratos` de `num_plantas`
(`apps/clientes/services/vistas.py:155-165`), pero lo hace solo sobre contratos
de servicio, donde la distinción casi no se nota. Al entrar PPA, sí se nota.

### (c) La misma fila aparece en varias plantas

Al ver los servicios **de un proyecto**, un PPA compartido aparece ahí igual que
en las otras 4 plantas. Conviene que la fila lo diga — *"cubre 5 plantas"* — para
que nadie lea su tarifa como si fuera de esa planta sola.

### Qué implica para el diseño

Lo que ya estaba previsto y sigue siendo correcto: **`plantas` es una lista** en
la forma común de lectura (§7), justamente por esto.

Lo que hay que agregar:

1. **Los totales se calculan por contrato, nunca sumando por planta.** Al
   agrupar, deduplicar por `(fuente, contrato_id)` antes de sumar.
2. **Cada conteo declara si cuenta contratos o plantas.** No mezclarlos.
3. **No repartir tarifas de PPA entre plantas** mientras no exista el dato. Si el
   negocio lo necesita, es una columna nueva en `ppa_contrato_proyectos` y una
   decisión aparte — no algo que esta agrupación deba inventar.

Nada de esto cambia el esquema ni la ruta de §9: cambia cómo se agrega y cómo se
rotula.

## 4-ter. Las banderas `srv_*`: la segunda fuente de verdad

`Proyecto` tiene cuatro banderas booleanas que **son exactamente estos grupos**:

```python
srv_operacion      = models.BooleanField(default=False)
srv_representacion = models.BooleanField(default=False)
srv_cgm            = models.BooleanField(default=False)
srv_ppa            = models.BooleanField(default=False)
```

**No están desconectadas: son criterio de filtro en lógica crítica.** Al menos
14 sitios del backend vivo dependen de ellas:

| Bandera | Quién la usa para decidir |
|---|---|
| `srv_operacion` | sondeo MGS y alarmas de desconexión (`apps/monitoreo`), panel de O&M, panel de arriendos, informe FMO (`api/v1/informe_om`), reconectadores, monitoreo SolarView, portafolios |
| `srv_representacion` | panel contable, panel de liquidaciones, piscinas de cumplimiento XM, registros CND |
| `srv_cgm` | orquestador del reporte de energía |
| `srv_ppa` | pipeline comercial |

Si una planta tiene `srv_operacion=False`, **el sondeo de monitoreo no la mira y
el informe FMO no la incluye**. La bandera no es decorativa.

**Lo que NO tienen es conexión con los contratos.** Nada las escribe
automáticamente: en `apps/` y `api/` todas las ocurrencias son lecturas. Se
editan a mano desde el formulario de Proyecto (`api/v1/proyectos/serializers.py`
es el único sitio que las expone para escritura). La revisión 118 menciona un
`_run_srv_operacion_sync()` que existió en FastAPI y era *"un no-op permanente"*.

**Hay entonces dos respuestas posibles a "¿qué servicios tiene esta planta?"**:

1. Las banderas `srv_*` del proyecto — puestas a mano
2. Los contratos en `contratos_servicio` / `ppa_contratos` — el hecho comercial

Y **nada garantiza que coincidan**. Tanto es así que dos módulos ya las concilian
explícitamente, aceptando cualquiera de las dos:

```python
# apps/contabilidad/services/panel.py:212 y apps/liquidaciones/services/panel.py:136
Q(srv_representacion=True) | Q(Exists(con_contrato))
```

El comentario en `contabilidad/panel.py:202` lo llama *"criterio SEGURO"* —
seguro precisamente porque no se fía de ninguna de las dos por separado.

### Decisión: una sola fuente de verdad

**Decidido (Sara, 2026-09-16): se va a una sola fuente de verdad, por fases.**
Se asume el riesgo a cambio de eliminar la doble verdad, que hoy obliga a cada
módulo a inventar su propio criterio de conciliación.

El riesgo concreto a administrar es uno solo, y conviene tenerlo escrito:

> Si una planta presta el servicio pero su contrato no está cargado, derivar la
> bandera la **apagaría**, y esa planta **dejaría de monitorearse y de salir en
> el informe FMO**. Un hueco de datos se volvería una planta sin vigilancia.

Por eso las fases de abajo están ordenadas de modo que **ninguna apague nada por
inferencia sin que un humano haya visto antes la lista de plantas afectadas.**

### Plan por fases

**Fase 0 — Medir.** Informe de solo lectura: para cada planta, su bandera y si
existe contrato del grupo. La salida es la lista de discrepancias en los dos
sentidos: bandera encendida sin contrato, y contrato sin bandera. Sin esto no se
puede dimensionar nada. *(Bloqueado por el acceso a la base.)*

**Fase 1 — Derivar en paralelo, sin cambiar comportamiento.** Se expone el valor
derivado de los contratos **al lado** de la bandera, sin que nadie lo consuma
todavía. Las banderas siguen mandando. Permite comparar las dos verdades sobre
datos reales de producción, en el tiempo, sin arriesgar nada.

**Fase 2 — Migrar los consumidores de bajo riesgo.** Los que solo pintan o
cuentan, donde equivocarse se ve y se corrige: pipeline comercial, paneles de
O&M y arriendos, panel contable, liquidaciones. Ninguno apaga un proceso.

**Fase 3 — Migrar los de alto riesgo, uno por uno.** Sondeo MGS, alarmas de
desconexión, informe FMO, piscinas de cumplimiento XM, registros CND. **Regla
para cada uno: antes de migrarlo, revisar a mano la lista de plantas que
cambiarían de estado.** Se migra uno, se observa, y solo entonces el siguiente.

**Fase 4 — Retirar las banderas.** Solo cuando ningún consumidor las lea y la
discrepancia de la Fase 0 esté en cero. Es una migración de Django que borra
cuatro columnas, y es el único paso irreversible.

### Medido el 2026-09-17: qué falta para poder borrar las banderas

`python manage.py revisar_servicios_banderas` (solo lee; `--csv` saca el detalle
planta por planta). **190 plantas vivas.**

| Bandera | Con bandera | Con contrato | **Bandera SIN contrato** | Contrato sin bandera |
|---|---|---|---|---|
| `srv_operacion` | 70 | 35 | **39** | 4 |
| `srv_representacion` | 57 | 63 | **3** | 9 |
| `srv_cgm` | 58 | 63 | **7** | 12 |
| `srv_ppa` | 21 | 39 | **5** | 23 |

**La columna que manda es "bandera SIN contrato".** Son las plantas que hoy
tienen el servicio marcado y ningún contrato que lo respalde: si se borra la
bandera, el servicio desaparece para el sistema. `contrato sin bandera` es lo
contrario y no hace daño — al derivar, esas plantas GANAN el servicio.

**La condición para borrar las cuatro columnas es que esos cuatro números
lleguen a cero.** El comando es el marcador: se corre cuando se quiera y dice
cuánto falta.

#### Las 39 de `srv_operacion`

**36 de ellas no tienen NINGÚN contrato** —ni de operación, ni de representación,
ni PPA—. Por estado: 33 `en_operacion`, 4 `en_desarrollo`, 2 `cancelado`.

Por los nombres, casi todas parecen autoconsumo sobre techo (IML Empaques, IML
Etiquetas, AMC, Nuevo Gimnasio, Almagran, Cross, Pola del Pub, MDM, San Simón,
Ecoimagen, IBES, Polikem, San Angelo, Cristo Rey, las Somer, Obelisco, Arboleda),
un tipo de planta distinto de las GD y las minigranjas.

Dos de ellas están **canceladas** con la bandera encendida (MGS Naos 2 y 3):
esas se apagan y ya.

#### Cómo se cierra el hueco

Por cada planta de esa lista hay **dos respuestas posibles, las dos de negocio**:

1. **Sí se le presta el servicio** → falta cargar el contrato en la plataforma.
2. **No se le presta** → la bandera está mal puesta y hay que apagarla.

En los dos casos el resultado es el mismo: bandera y contrato coinciden, y **la
bandera deja de aportar información**. Ahí se puede borrar.

Es trabajo de datos, no de código, y lo hace quien conoce los contratos. El CSV
del comando (`--csv`) es la hoja de revisión.

### Lo que falta definir antes de la Fase 1

## 4-quater. Criterio de éxito: qué duplicación tiene que desaparecer

**Requisito de Sara (2026-09-16): este enfoque no puede agregar lógica repetida.
Al contrario, tiene que consolidar la que ya existe.**

Eso es medible. Hoy las mismas preguntas se responden en varios sitios, **y con
respuestas distintas**. Inventario real:

### "¿Esta planta está en operación?" — 4 definiciones, e incompatibles

```python
apps/om/services/panel.py:64          estado="en_operacion" AND srv_operacion=True
apps/arriendos/services/panel.py:79   estado="en_operacion" AND srv_operacion=True
apps/proyectos/services/portafolios.py:30   srv_operacion=True  OR  estado="en_operacion"
apps/energia/.../solarview_monitoreo.py:453  ... AND tipo_proyecto="minigranja"
```

Nótese **`AND` en dos sitios y `OR` en el otro**. No son matices de estilo:
devuelven conjuntos de plantas distintos. Una planta con la bandera encendida
pero sin estado `en_operacion` entra en portafolios y no entra en el panel de
O&M. Nadie decidió eso; es divergencia acumulada.

### "¿Esta planta está representada?" — 4 definiciones, dos criterios

```python
apps/contabilidad/services/panel.py:212   srv_representacion OR existe contrato
apps/liquidaciones/services/panel.py:136  srv_representacion OR con_contrato
apps/mercado_xm/.../piscinas.py:53        srv_representacion  (solo la bandera)
apps/registros_cnd/services/registros.py:283  srv_representacion  (solo la bandera)
```

Dos módulos concilian con el contrato y dos se fían solo de la bandera. Para la
misma planta, contabilidad puede considerarla representada y cumplimiento XM no.

### "¿Cuál es la tarifa de este servicio?"

Una cadena de `if` sobre `servicio_aplica` en
`apps/clientes/services/vistas.py:126-133`, sin equivalente reutilizable.

### Lo que ya está bien y sirve de modelo

`semaforo_contrato()` y `peor_semaforo()` viven una sola vez en
`apps/clientes/services/panel.py` y se consumen desde cuatro sitios. **Ese es el
patrón a replicar**, no uno a inventar.

### La regla de verificación

La propuesta **no es una capa encima de lo que hay**: es el lugar donde estas
definiciones divergentes se unifican. Se verifica contando:

> Al terminar cada paso, el número de sitios con filtros literales sobre
> `servicio_aplica`, `srv_*` y `estado="en_operacion"` tiene que ser **menor**
> que antes, nunca mayor.

Hoy la línea base es de **~15 filtros literales de `servicio_aplica`** repartidos
por `apps/`, más las 8 definiciones divergentes de arriba.

Si un paso agrega un módulo nuevo sin retirar filtros existentes, ese paso está
incompleto: falta migrar a sus consumidores.

### Dónde se consolida cada cosa

| Pregunta | Dónde va a vivir, una sola vez |
|---|---|
| Qué grupo y subservicios tiene un contrato | catálogo de grupos (`apps/contratos`) |
| Qué tarifa aplica según el subservicio | el mismo catálogo |
| Qué servicios tiene una planta | el derivado de contratos (§4-ter, Fase 1) |
| Si una planta está en operación | **una** definición, reemplazando las 4 |
| Si una planta está representada | **una** definición, reemplazando las 4 |
| Vigencia y semáforo | ya resuelto — `apps/clientes/services/panel.py` |

**Unificar las dos definiciones divergentes cambia comportamiento**: hay plantas
que hoy entran en un módulo y no en otro. Cuál de las variantes gana es decisión
de negocio, y hay que verla con la lista de plantas afectadas delante — el mismo
método de la Fase 3 en §4-bis.

## 5. Cómo estructurarlo — dos opciones

### Opción A — Derivar los subservicios al leer (sin cambio de esquema)

El grupo y los subservicios se calculan en la lectura:

- **PPA** → subservicio = `tipo_contrato`
- **Representación y CGM** → `tarifa_representacion` no nula ⇒ representación;
  `tarifa_cgm` no nula ⇒ cgm; ambas ⇒ los dos
- **Operación** → subservicio = `servicio_aplica`

`servicio_aplica` se queda quieto y **ninguno de los ~15 filtros literales
repartidos por `apps/` se entera**.

- **A favor:** sin migración, sin backfill, reversible, entrega valor en días.
- **En contra:** la verdad queda implícita. "Tiene `tarifa_cgm`" no es lo mismo
  que "se contrató CGM" — una tarifa en cero o pendiente de cargar rompe la
  inferencia.

### Opción B — Tabla explícita `contrato_subservicio`

Una tabla `(contrato_id, subservicio)` con unique en el par. Un contrato de
representación+CGM tiene dos filas; los demás, una.

- **A favor:** dice la verdad, sin inferencia. Deja la puerta abierta a más
  combinaciones sin volver a tocar el esquema.
- **En contra:** necesita migración, backfill, y **escritura en el wizard**. Sin
  eso es `servicio_representacion` otra vez (ver §2b).

### Recomendación

**A primero, B después si A no alcanza.** Razones:

1. A se puede montar y ver funcionando sin tocar producción ni el wizard.
2. Al implementar A se mide cuántos contratos caen en cada caso — que es
   justamente el dato que hoy no tenemos (ver §8).
3. Si esa medición muestra que la inferencia por tarifas falla seguido, B queda
   justificada con números, no con intuición. Y A deja el terreno listo: el
   contrato de API no cambia, solo cambia de dónde sale el subservicio.

**En ninguna de las dos se toca `servicio_aplica`, ni se mueven tablas entre
apps, ni PPA se fusiona con `contratos_servicio`.**

## 6. PPA se queda con tabla propia

`PpaContrato` **no es un detalle colgado de un contrato: es el contrato**, con
contraparte propia, tarifas escalonadas, compromisos de energía y varias plantas.
Meterlo en `contratos_servicio` obligaría a agregarle columnas que solo él usa —
exactamente el problema que hoy tiene internet.

Se une a los demás **en la capa de lectura**, no en la de datos.

## 7. Cómo se estructuraría

### Backend

**Paso 1 — El catálogo de grupos.** Un módulo nuevo en `apps/contratos` con el
mapa grupo → subservicios y el orden de presentación. Hoy ese conocimiento está
regado en `if`s (`apps/clientes/services/vistas.py:126-133`) y en filtros
literales por todo `apps/`. Archivo nuevo, no rompe nada.

**Paso 2 — Una forma común de servicio.** Un servicio expuesto es:

```
grupo, subservicios[], contrato_id, fuente ("servicio" | "ppa"),
plantas[], vigencia, estado, semaforo, tarifas{}, enlace, partes{}
```

`ContratoServicio` la llena desde sus columnas; `PpaContrato` desde las suyas.
`subservicios` es **una lista** — ahí entra el caso representación+CGM, y los
demás traen un solo elemento. `plantas` es lista porque PPA admite varias.

### Las partes NO se uniforman

**Decisión de Sara (2026-09-16):** `comprador`/`vendedor` de un PPA y
`contratante`/`prestador` de un contrato de servicio **conservan sus nombres**.
No se reducen a una "contraparte" genérica.

No son sinónimos. En un PPA dicen quién compra energía y quién la vende; en un
contrato de servicio, quién paga el servicio y quién lo presta. Un campo común
borraría esa diferencia, que es justamente lo que define la relación.

El sistema ya lo trata así: en el front, `FiltrosPpa` (comprador/vendedor) es
una interfaz **separada** de `FiltrosServicio`, y las tres vistas tienen sus
propias columnas.

Por eso `partes{}` es un bloque cuyo contenido depende de `fuente`:

```
fuente = "ppa"        → partes = { comprador, vendedor }
fuente = "servicio"   → partes = { contratante, prestador }
```

**Esto no contradice la regla de §4-quater.** Lo que no puede repetirse es la
*lógica* — la misma pregunta respondida de dos formas distintas, como las 4
definiciones de "planta en operación". Dos campos que se llaman distinto porque
**significan** cosas distintas no son duplicación: son dos datos.

### El criterio para decidir qué va en común

Va a la forma común **solo lo que sirve para agrupar, contar o filtrar de forma
transversal**: grupo, subservicios, plantas, vigencia, estado, semáforo, enlace.
Todo lo específico de un tipo de contrato se queda donde está, con su nombre.

**Paso 3 — El endpoint agrupado.** `GET /api/v1/servicios` devolviendo los tres
grupos con sus subservicios, conteos y semáforo. Los endpoints actuales
(`/contratos-servicio`, los de PPA) **siguen igual** — esto se agrega al lado,
no los reemplaza.

### Frontend

Las tres vistas ya existen y no hay que crearlas. El cambio es de **origen del
dato**:

- `filtrosServicios.ts` deja de filtrar por `servicio_aplica` con igualdad y pasa
  a preguntar **si el subservicio está en la lista**. Es un cambio acotado en
  `filtrarServicios`, y el archivo ya tiene pruebas (`filtrosServicios.test.ts`).
- `ServiciosUnificadoView.vue` puede seguir pidiendo la lista plana como hoy
  (nada se rompe) y adoptar el endpoint agrupado cuando convenga. Tiene 1838
  líneas y está fuera del lint: conviene tocarlo lo mínimo.
- El wizard no se toca en esta fase.

## 8. La medición (bloque B1, hecha el 2026-09-17)

Corrida contra producción (`34.74.198.101`, base `operaciones`), solo lectura.
**160 contratos de servicio y 33 PPA.**

### Por `servicio_aplica`

| valor | contratos | con `tarifa_representacion` | con `tarifa_cgm` |
|---|---|---|---|
| representacion | 94 | 91 | 92 |
| mantenimiento | 31 | — | — |
| arriendo | 31 | — | — |
| internet | 4 | — | — |
| **cgm** | **0** | — | — |

### Pregunta 1: ¿representación+CGM es una fila o dos? — **UNA fila**

- **91 contratos tienen las dos tarifas**, todos etiquetados `representacion`.
- **0 contratos con `servicio_aplica='cgm'`.**
- **0 plantas** con contratos separados de representación y CGM.

Desglose de los 94: **91 cubren ambos**, 2 solo representación, **1 solo CGM**
(etiquetado `representacion` pero sin `tarifa_representacion` — invisible al
filtrar por cualquiera de los dos hasta ahora).

**Consecuencia:** la Opción A de §5 basta y **no hay que consolidar filas**. El
paso C3 se cierra sin trabajo. Y agregar `cgm` a lo que la vista le PIDE al
backend sería inútil: devolvería 0 filas, el mismo error que ya costó
`?tipo=operacion` (§2).

### Pregunta 2: ¿internet merece tabla propia? — **no con estos números**

- 4 contratos de internet, los 4 con columnas técnicas llenas.
- **0 contratos de otro tipo** con esas columnas sucias.

Las 13 columnas están limpias y afectan a 4 filas. **E2 baja de prioridad**: el
costo (migración + reescribir el wizard) no se paga con 4 filas.

### PPA

- 33 contratos: **12 compra, 21 venta**.
- **3 cubren más de una planta**; **10 no tienen ninguna planta vinculada** —
  esos no salen al filtrar por `proyecto_id`.
- 39 plantas cubiertas en total, contra 33 contratos: la distinción
  contratos/plantas de §4-bis no es teórica.

### Lo que devuelve el endpoint con datos reales

| grupo | contratos | plantas |
|---|---|---|
| ppa | 33 | 39 |
| representacion_cgm | 94 | 64 |
| operacion | 66 | 35 |

### Lo que sigue sin medir

Las **definiciones divergentes de §4-quater** (`AND` vs `OR` en "planta en
operación", bandera con o sin contrato en "planta representada") y la
**discrepancia banderas `srv_*` vs. contratos**. Son las que el bloque C y el D
necesitan, y hay que medirlas antes de unificar nada.

## 9. Orden propuesto

Cinco bloques. **Antes de empezar cada paso se acuerda el enfoque con preguntas
concretas**; no se implementa nada sin ese acuerdo.

### Bloque A — Fundamentos (sin base de datos, sin riesgo)

| # | Qué | Riesgo |
|---|---|---|
| A1 | ✅ Catálogo de grupos, subservicios y tarifa por subservicio | Ninguno — archivo nuevo |
| A2 | ✅ Forma común de lectura: servicio unificado, PPA incluido, `plantas` como lista | Bajo — no toca endpoints actuales |
| A3 | ✅ Endpoints `GET /api/v1/servicios` y `/servicios/catalogo` | Bajo — se agregan al lado |
| A4 | Front: filtrar por lista de subservicios en vez de igualdad | Bajo — `filtrosServicios.ts` tiene pruebas |

**Hecho al 2026-09-16** (sin commitear, sin desplegar):

- `apps/contratos/services/grupos.py` — el catálogo (A1)
- `apps/contratos/services/unificado.py` — forma común y agrupación (A2)
- `apps/contratos/services/consulta.py` — consulta con filtro opcional (A3)
- `api/v1/servicios/` — los dos endpoints (A3)
- Pruebas: `tests/test_contratos_grupos_servicio.py` (22),
  `tests/test_contratos_unificado.py` (20), `tests/test_servicios_agrupados.py` (14)

Filtros literales retirados: la cadena de `if` servicio→tarifa de
`apps/clientes/services/vistas.py`, que ahora consume el catálogo.

Al cerrar el bloque, la vista Servicios funciona con los tres grupos y el caso
representación+CGM deja de ser invisible. **Nada de producción cambia de
comportamiento.**

### Bloque B — Medición (necesita la base)

| # | Qué | Riesgo |
|---|---|---|
| B1 | Informe de discrepancias, solo lectura | Ninguno |

Un solo informe que responde las cuatro preguntas abiertas: si representación+CGM
está en una fila o dos, cuántos contratos de internet tienen datos, dónde
bandera y contrato no coinciden, y qué plantas cambiarían con cada una de las
definiciones divergentes de §4-quater. **Sin esto no se abre ningún bloque
siguiente.**

### Bloque C — Unificar las definiciones divergentes (cambia comportamiento)

| # | Qué | Riesgo |
|---|---|---|
| C1 | Una sola definición de "planta en operación" (hoy 4, con `AND` vs `OR`) | Medio — con lista revisada |
| C2 | Una sola definición de "planta representada" (hoy 4, dos criterios) | Medio — con lista revisada |
| C3 | Consolidar filas representación+CGM, **si** B1 muestra que están separadas | Medio |

### Bloque D — Fuente única de verdad (§4-ter)

| # | Qué | Riesgo |
|---|---|---|
| D1 | Derivar el valor en paralelo, sin que nadie lo consuma | Ninguno |
| D2 | Migrar consumidores de bajo riesgo (paneles, pipeline comercial) | Bajo |
| D3 | Migrar monitoreo, alarmas, FMO, XM, CND — **uno por uno** | **Alto** |
| D4 | Borrar las cuatro columnas `srv_*` | Irreversible |

### Bloque E — Opcionales, solo si los números lo justifican

| # | Qué | Riesgo |
|---|---|---|
| E1 | Tabla `contrato_subservicio` (Opción B de §5) | Medio — migración + wizard |
| E2 | Internet a tabla propia | Medio — el wizard escribe esas columnas |

### Regla transversal

Al cerrar **cada** paso se cuenta la línea base de §4-quater: los sitios con
filtros literales sobre `servicio_aplica`, `srv_*` y `estado="en_operacion"`
tienen que ser **menos** que al empezar. Un paso que agrega un módulo sin
retirar filtros está incompleto.

## 10. Lo que esta propuesta NO hace

- No toca `servicio_aplica` ni su enum.
- No fusiona PPA con `contratos_servicio`.
- No mueve las tablas `om_*` ni `arr_*`.
- No cambia los endpoints existentes.
- No toca el wizard.
- **No apaga ninguna bandera `srv_*` por inferencia** sin que alguien haya visto
  antes la lista de plantas que cambiarían (§4-ter, Fase 3).
- No migra ni escribe un solo dato de producción.
