# Monitoreo — backend y base de datos

> **Qué es esto.** Una radiografía del backend de la sección de Monitoreo de la
> plataforma de Operaciones: qué tablas existen, cómo se relacionan de verdad, qué
> está inconsistente y qué decisiones ya se tomaron. Se escribió como paso previo a
> reestructurar el modelo de datos.
>
> **Alcance.** Las tres vistas que sostienen el trabajo diario de operación:
> `/fallas`, `/solar-live` y `/operaciones/informes-mensuales`.
>
> **Fecha.** 23 de agosto de 2026. Todo lo que dice de la base salió del snapshot del
> esquema de producción en `esquema-bd-produccion/esquema.json`, no de leer los
> modelos. Lo que dice del código está referenciado con `archivo:línea`.
>
> **Para quién.** Cualquiera que vaya a tocar fallas, monitoreo solar o informes
> mensuales. No hace falta contexto previo.
>
> **Qué NO es.** No es la arquitectura completa del backend: cubre el dominio de
> monitoreo. Para la vista transversal de las 6 bases de datos de Unergy está
> `docs/UNERGY_DATABASE_ATLAS.md`; para la auditoría general, `docs/DB_REVIEW_TEAM.md`.
>
> **Convención de rutas.** Las rutas sin prefijo (`app/...`, `docs/...`) son de **este
> repo** (`unergy-operaciones-backend`). Las que llevan `[frontend]` son del repo
> `unergy-operaciones-frontend`.

---

## 1. El resumen en diez líneas

- Todo el dominio cuelga de una sola tabla: **`proyectos`** (61 columnas, 38 claves
  foráneas entrantes).
- Hay **24 tablas** implicadas y **31 claves foráneas reales** entre ellas.
- Hay **2 relaciones que no son claves foráneas sino comparación de texto**, y son
  justo las que más daño hacen.
- **No existe una entidad «equipo».** Hay cuatro representaciones parciales del mismo
  activo físico, en cuatro tecnologías distintas, que no se hablan entre sí.
- El racimo de fallas está bien modelado. El de informes está desconectado del núcleo.
- Se detectaron **23 problemas** de integridad, eficiencia o corrección; 10 de
  severidad alta.
- Ya se retiraron tres vistas y dos routers que nadie usaba (ver §6).
- La decisión de fondo —cómo modelar los equipos— **está abierta** (ver §7).

---

## 2. Las tablas, por racimo

Cada «racimo» es una actividad del negocio. Todos cuelgan de `proyectos` por
`proyecto_id`.

### Núcleo

| Tabla | Col. | Para qué |
|---|---|---|
| `proyectos` | 61 | La planta. De sus 61 columnas, estas vistas leen 15 (ver §3.1). |
| `usuarios` | 11 | Quién registra, atiende, aprueba y envía. |
| `clientes` | — | Dueño/inversionista; de aquí salen los contactos del informe. |

### Atender una falla

| Tabla | Col. | Para qué |
|---|---|---|
| `fallas` | 38 | El evento. 7 FK salientes, 3 entrantes, 20 índices. |
| `fallas_seguimientos` | 6 | Bitácora y cada cambio de estado. |
| `fallas_intervalos` | 6 | Tramos de afectación dentro de una falla larga. |
| `falla_inversores` | 7 | **La única tabla puente entre una falla y un aparato concreto.** |
| `fallas_cat_categorias` | 7 | Catálogo raíz de la clasificación estructurada. |
| `fallas_cat_tipos` | 6 | Subtipo, colgado de la categoría. |
| `fallas_cat_estados` | 6 | `es_estado_final` gobierna toda la lógica de «sigue abierta». |
| `fallas_cat_prioridades` | 5 | `nivel` elige el SLA por defecto. |
| `fallas_cat_resoluciones` | 3 | Cómo se cerró. |
| `proyecto_inversores` | 12 | Los inversores de la planta. **El único registro de activos real.** |
| `proyecto_inversionistas` | 10 | Vínculo planta ↔ cliente con vigencia. |

### Medir generación

| Tabla | Col. | Para qué |
|---|---|---|
| `generacion_diaria` | 10 | kWh reales por planta y día + `kwh_p90` como línea base. |
| `fronteras` | 101 | La frontera comercial: código SIC, operador, medidor. |
| `alarmas_monitoreo` | 8 | Alarmas del detector de desconexión. **Tabla isla.** |

### Emitir el informe

| Tabla | Col. | Para qué |
|---|---|---|
| `informes_guardados` | 25 | El informe entero como HTML + el circuito editorial. |
| `contactos` | 9 | Correos por cliente y área. |
| `proyecto_area_contacto` | 6 | Puntero por área: decide a quién llega el informe. |
| `email_envios` | 10 | Bitácora de correos enviados. **Tabla isla.** |
| `contratos_servicio` | 61 | Contrato de O&M: número, tarifa, vigencia. |
| `portafolios` | 6 | Agrupación de plantas para el consolidado. |
| `proyecto_inicio_operacion` | 11 | Ficha de puesta en marcha (checklist en JSONB). |

---

## 3. Las relaciones reales

### 3.1 Lo que estas vistas leen de `proyectos`

De sus 61 columnas se usan **15**:

- **Cruce (6)** — `id`, `sub_project` (puente con la API Unergy),
  `project_id_solenium` (puente con Solenium), y **tres** alias de nombre:
  `nombre_comercial`, `nombre_clientes`, `nombre_bitacora`.
- **Universo (3)** — `estado` (`= 'en_operacion'`), `tipo_proyecto` (`= minigranja`),
  `srv_operacion` (`= true`).
- **Cálculo (4)** — `potencia_instalada_kwp` (es AC, no DC) y las curvas
  `p50_mensual_kwh`, `p90_mensual_kwh`, `p99_mensual_kwh`.
- **Ficha del informe (2)** — `municipio`, `departamento`.

> Las tres columnas de nombre existen **para que el cruce por texto acierte más
> veces**. No es interpretación: el esquema lo documenta en `ProyectoResumen`
> (`app/schemas/fallas.py:61`) — *«alias adicionales para cruzar la falla con
> proyectos en informes/monitoreo»*.

### 3.2 Las dos relaciones que NO son claves foráneas

Estas dos son la causa raíz de varios bugs. Ninguna existe como restricción en la
base:

| Origen | Se une por | Destino | Consecuencia |
|---|---|---|---|
| `informes_guardados.sub_project` | texto | `proyectos.sub_project` | `informes_guardados` **no tiene ninguna FK hacia `proyectos`**. Sus 4 FK apuntan todas a `usuarios`. |
| `alarmas_monitoreo.proyecto_nombre` | texto | `proyectos.nombre_comercial` | La alarma guarda el nombre como cadena. No se puede unir por id. |

`alarmas_monitoreo` y `email_envios` no tienen **ninguna** clave foránea, ni entrante
ni saliente. Son islas.

### 3.3 Regla de borrado

De las 31 FK, la mayoría no declara `ON DELETE`, así que la base **impide** borrar el
padre mientras exista el hijo. Las excepciones:

- `generacion_diaria.proyecto_id → proyectos` — `ON DELETE CASCADE`.
- Las 4 FK de `informes_guardados` hacia `usuarios` — `ON DELETE SET NULL`.
- `contratos_servicio.contratante_id` / `prestador_id → clientes` — `ON DELETE SET NULL`.

---

## 4. La inconsistencia estructural: el equipo no existe

Es el hallazgo más importante del análisis. **La plataforma no tiene una entidad
«equipo».** Tiene cuatro representaciones parciales del mismo activo físico:

| # | Dónde | Forma | Qué cubre |
|---|---|---|---|
| 1 | `proyecto_inversores` | Tabla real | Solo inversores, y solo `nombre`, `potencia_nominal_kw`, `orden`, `activo`. **Marca, modelo y numero de serie ya no existen**: los borro la revision 113 de Alembic (2026-08-27) porque estaban en **0 de 675 filas**. No hay serial de ningun equipo en la plataforma. |
| 2 | `fronteras` | Tabla real | El punto de medida comercial, **no** los aparatos. |
| 3 | `proyecto_inicio_operacion.checklist` | JSONB | 21 tipos de equipo, como casillas de verificación. |
| 4 | `ESTRUCTURA_FALLAS` | Lista de Python | Los mismos equipos otra vez, como cadenas de texto. |

**Ejemplo concreto.** El medidor de respaldo de una planta existe como la clave
`checklist.frontera.respaldo`, como la cadena `"medidor_respaldo"` en
`app/services/fallas/estructura.py`, y como columnas sueltas entre las 101 de
`fronteras`. **En ninguna parte es una fila** que una falla pueda referenciar.

### 4.1 El inventario ya existe — como lista de chequeo

`proyecto_inicio_operacion.checklist` es un JSONB que ya cubre **21 tipos de equipo**:
paneles, tracker, inversores, estación meteo (con POA, temperatura, velocidad y
dirección del viento), reconectador, Starlink, Fusion Solar, medidor principal y
respaldo, CCTV, cable solar, cableado MT/BT, transformadores, tableros, shelter/skid,
obras civiles, documentación O&M.

El comentario del modelo es explícito: *«el catálogo de ítems lo define el frontend,
el backend solo persiste el estado»* (`app/models/inicio_operacion.py`).

> **Ojo.** Ese catálogo estaba escrito en `[frontend] InicioOperacionView.vue`, vista que se
> retiró (ver §6). Hoy solo sobrevive en el historial de git (commit `f74d9b1~1`) y,
> parcialmente, en las funciones de progreso de `app/api/v1/inicio_operacion.py` que
> siguen leyendo esas claves para el Informe O&M.

### 4.2 Un inversor tiene tres identidades

El inversor es el único equipo bien modelado y aun así está partido en tres:

1. **`proyecto_inversores.id`** — nuestra fila. Es a la que apunta
   `falla_inversores.proyecto_inversor_id`, o sea la identidad del historial de fallas.
2. **El id de Solenium** — la ficha de puesta en marcha no lee nuestra tabla: llama a
   Solenium en vivo y usa *su* id para indexar el checklist de strings
   (`checklist.inversores.items[<id_solenium>]`, ver `inicio_operacion.py:210`).
3. **`dev_name`** — en esa misma ruta la potencia nominal se obtiene aplicando una
   expresión regular al **nombre** del dispositivo.

**Nada conecta las tres.** Si un técnico aprueba los strings del «Inversor 3» y tres
meses después ese inversor falla, no hay forma de saber que es el mismo aparato:
la aprobación está bajo el id de Solenium y la falla bajo el nuestro.

### 4.3 No existe el tramo de red

`operadores_red` guarda la empresa y sus contactos, nada más. No hay tramo, ni nodo,
ni circuito.

Un evento de red es hoy una falla de categoría `red` sobre **un** proyecto. Si se cae
un tramo que alimenta cinco plantas, eso son cinco fallas independientes sin ningún
vínculo. Lo mismo con un huracán que golpea ocho plantas: ocho fallas sueltas.

### 4.4 Lo que el modelo no puede responder

| Pregunta | ¿Se puede? |
|---|---|
| ¿Qué inversor de esta planta ha fallado más este año? | Sí |
| ¿Cuántas fallas de red tuvo esta planta? | Sí |
| ¿Qué medidor ha fallado más en toda la flota? | No — el medidor no es una fila |
| ¿Cuántas veces se ha reemplazado este inversor? | No — la tabla guarda estado, no historia |
| Este corte de red, ¿a cuántas plantas afectó? | No — no existe el evento |
| ¿Qué plantas cuelgan del mismo circuito? | No — solo sabemos el operador |
| ¿Está en garantía el equipo que falló? | No — no hay fecha de instalación ni garantía |
| ¿Cuándo se le hizo mantenimiento, y cuándo toca el siguiente? | No — `mantenimientos` se eliminó el 2026-09-07: 0 filas y **ninguna forma de crear un registro** (ver la migración `0006_eliminar_mantenimientos`). Nunca tuvo intervalo ni vínculo con el equipo |

---

## 5. Bugs y riesgos detectados

23 en total, 10 de severidad alta. Los estructurales primero; el resto agrupado.

### 5.1 Se cruza por nombre donde existe un id

Aparece cuatro veces: el emparejamiento con Solenium
(`generacion_solar.py:93–123`) hace *match por subcadena* y devuelve el primer
acierto del recorrido del diccionario — con «San Pelayo 1» y «San Pelayo 2» puede
atribuir la generación a la planta equivocada. Las fallas del informe, los miembros
del portafolio y el destinatario del correo se resuelven igual.

**Palanca de arreglo:** que `_action_get_projects` (`monitoreo.py`) devuelva `p.id`.
Con eso se pueden cruzar las fallas por `proyecto_id` en vez de por nombre.

### 5.2 El destinatario del correo sale de un `LIMIT 1` sin orden

`informes.py:139–152` busca `WHERE sub_project = :sp OR nombre_comercial = :sp LIMIT 1`
sin `ORDER BY`. Si el `sub_project` de una planta coincide con el `nombre_comercial`
de otra, el informe puede salir al cliente equivocado.

### 5.3 Nada impide reenviar un informe al cliente

`POST /informes/{id}/enviar` valida estado y permisos pero **no consulta
`correo_enviado`** — campo que existe y se marca *después* del envío
(`informes.py:622`, `:666`). Un doble clic o un reintento reenvía.

### 5.4 Un mes verificado a medias se reporta casi en cero

El puente pide primero solo lecturas con `verified_by_operator=True` y solo cae a las
no verificadas si la lista vuelve **completamente** vacía (`monitoreo.py:123–125`). Si
el operador verificó la primera semana, las otras tres se reportan en cero y nada
avisa.

### 5.5 Reabrir un informe aprobado no borra la firma

`informes.py:458` reasigna `inf.estado = payload.estado`; trece líneas después,
`:469` pregunta `if ... and inf.estado == "aprobado"` para limpiar los campos de
aprobación. La condición nunca se cumple: **es código muerto**. Un informe devuelto a
borrador sigue mostrando quién lo aprobó y cuándo.

### 5.6 Hay tres formas distintas de calcular «cuánto debió generar»

- Factor de planta 0,18 sobre la potencia — `fallas.py:313`
- `generacion_diaria.kwh_p90` — `services/impact_calculator.py:106`
- `p90_mensual_kwh ÷ días del mes` — en las vistas

Dan cifras distintas para el mismo evento y ninguna pantalla dice cuál usa.

### 5.7 El job de generación descarta los días en cero

`main.py:2426` solo inserta la fila `if kwh > 0`. Una planta caída todo el día no deja
fila, así que la base **no puede representar «este día generó cero»** — justo el día
que importa para calcular pérdidas. Además el job arma su ventana con `date.today()`
(UTC del contenedor) mientras el lector consulta con `_hoy_col()` (UTC−5).

### 5.8 La cadena de impacto produce nulos — **RESUELTO** (2026-09-07)

Se eliminó `mantenimiento_impacto` y todo su código. El diagnóstico era: la energía
esperada salía de `generacion_diaria.kwh_p90`, que **no la llena ningún job** — solo un
script manual y un endpoint de carga —, y el cálculo corría una sola vez **al crear** la
falla, nunca al resolverla. `lost_energy_kwh`, `financial_impact_cop` y la bandera de
riesgo PPA quedaban vacíos para toda falla.

No se arregló: se retiró. La tabla tenía **0 filas** y ningún consumidor — ni la bandera
`generar_impacto`, ni los 5 endpoints de `/mantenimiento-impacto`, ni los 4 campos que
`cumplimiento/resumen.py` derivaba de ahí aparecían en el frontend. El razonamiento
completo y qué haría falta para reponerlo están en
`apps/monitoreo/migrations/0005_eliminar_mantenimiento_impacto.py`.

Ojo: **el impacto que la UI muestra hoy no era este**. `fallas.kwh_perdidos_estimado` sale
de `fallas/dominio.py::estimar_perdida` (placa × 0.18 × horas × 800 COP), una regla de dedo
que no mira medición real — y se llena a mano desde la edición rápida del detalle.

### 5.9 Un GET que escribe y congela un número provisional

`GET /fallas/{id}/impacto` calcula la pérdida con `end = fecha_resolucion or now()` y
la persiste si el campo está vacío (`fallas.py:1124`). Con la falla abierta ese
`now()` es el instante en que alguien abrió la pantalla, y como después el campo ya
tiene valor **nunca se recalcula**. También `GET /monitoring` escribe: auto-asigna
`project_id_solenium` (`generacion_solar.py:1154`).

### 5.10 El SLA se ancla a medianoche

El vencimiento se construye desde `fecha_identificacion` a las 00:00 sin sumar
`hora_identificacion` —que sí se captura y se guarda—. Una falla detectada a las 18:00
con SLA de 24 h vence a medianoche: seis horas reales en vez de veinticuatro. Para una
crítica (SLA 8 h) identificada pasadas las 8 a.m., nace vencida.

SLA por defecto (`fallas.py:299`): crítica 8 h · alta 24 h · media 72 h · baja 168 h.
`sla_cumplido` es siempre calculado, nunca editable, y es `NULL` mientras la falla
está abierta.

### 5.11 El resto

| # | Qué | Dónde |
|---|---|---|
| — | El bucket «Alerta SLA» de la vista ignora la prioridad: en la práctica significa «lleva 7+ días abierta» | `[frontend] MonitoreoView.vue:828` |
| — | El drawer de falla se alimenta del listado, que no trae seguimientos → la bitácora siempre sale vacía | `[frontend] MonitoreoView.vue:1163` |
| — | La vista descarga todas las fallas paginando en paralelo, con `ORDER BY created_at` sin desempate → puede duplicar u omitir filas | `[frontend] MonitoreoView.vue:966` |
| — | `toISOString()` sin corregir UTC−5: después de las 19:00 los gráficos de /fallas se corren un día | `[frontend] MonitoreoView.vue:1031` |
| — | Dos números distintos de «generación de hoy» en solar-live: tarjetas leen tabla local, el bloque superior lee Solenium en vivo | `generacion_solar.py:1161` vs `:368` |
| — | El total de flota suma inversores con medidores según cuál respondió primero | `generacion_solar.py:453` |
| — | Una planta ausente de la respuesta de Solenium se reporta «sin comunicación», no «sin dato» | `generacion_solar.py:1182` |
| — | La disponibilidad del informe cuenta 07:00–17:59 contra un denominador 07:00–17:00 → puede pasar de 100 % | `[frontend] InformesMensualesPanel.vue:544` |
| — | Los miembros del portafolio que no cruzan por nombre se descartan en silencio | `[frontend] InformesMensualesPanel.vue:1457` |
| — | Tras un hueco de datos, todo el acumulado se imputa al primer registro que vuelve | `monitoreo.py:70` |
| — | La serie cruda se pide con `limit=10000` sin paginar ni verificar truncamiento | `monitoreo.py:52` |
| — | Disponibilidad garantizada fija en 97 % para todo contrato | `monitoreo.py:236` |
| — | Lo aprobado y lo enviado pueden diferir: el portafolio se recompone con los individuales vivos al enviar | `informes.py:185` |

---

## 6. Decisiones ya tomadas y desplegadas

### 6.1 Vistas retiradas

| Vista | Motivo | Commit |
|---|---|---|
| `/operaciones/informes-mensuales/dashboard` | Sin uso. Único consumidor de `[frontend] reportAggregatorService.js`. | `f74d9b1` |
| `/alertas/monitoreo` | Sin uso. Única pantalla web que consumía `/mgs/*`. | `f74d9b1` |
| Pestaña Costos Variables | Sin uso. | `f74d9b1` |
| `/operaciones/inicio-operacion` | Retirada a petición. | `9ef45b1` |

`/operaciones/costos-variables` no era una vista sino un contenedor de tres pestañas.
«Inicio de Operación» e «Informe de Puesta en Marcha» eran alcanzables solo desde ahí,
así que se movieron a ruta propia antes de borrar el contenedor.

### 6.2 Backend retirado

| Qué | Commit |
|---|---|
| `app/api/v1/mgs.py` (router de `/mgs/*`) | `7c4f2bc` |
| `app/api/v1/costos_variables.py` + su schema | `7c4f2bc` |
| Desmontado el include de `/inicio-operacion` (el módulo se conserva) | `c5b00ca` |

### 6.3 Lo que NO se borró, y por qué

- **`app/services/mgs/`** — pese al nombre no es el router: son los clientes de
  Solenium, Gaia y Quoia, importados por 25 módulos.
- **Modelo y tabla `costos_variables`** — hay datos en producción y el nombre está en
  `_MERGE_SIMPLE` (`proyectos.py:730`), que usa la fusión de proyectos duplicados.
- **`app/api/v1/inicio_operacion.py`** — `informe_om.py` le importa **siete helpers en
  uso**. Borrarlo tumba el arranque de la app entera. Solo se retiró su superficie HTTP.
- **`alarmas_monitoreo` + el detector** — `dashboard.py:77` cuenta las alarmas leyendo
  la tabla directo.

### 6.4 Decisiones de modelado ya acordadas

- **Inversores: sí llevan registro individual.** Ya lo tienen (`proyecto_inversores`)
  y es el patrón a generalizar, no a reemplazar.
- **Paneles: NO se registran unidad por unidad.** Una minigranja de 990 kWp tiene ~2 000
  paneles; inventariarlos uno a uno no aporta y es inmantenible. Se manejan por
  conjunto o por string.
- **Granularidad variable por tipo de equipo.** Inversores, medidores, reconectador y
  estación meteo merecen identidad individual; paneles, tableros, cableado y obras
  civiles se manejan como conjunto.
- **Los tramos de red van en tabla aparte.** Un proyecto se conecta a un tramo y un
  tramo puede alimentar varios proyectos, de modo que un corte sea **un** evento y no
  N fallas sueltas.

---

## 7. Decisiones pendientes

### 7.1 La abierta: ¿se conserva el equipo que sale?

Cuando se reemplaza un inversor, hay dos modelos:

- **Registro de estado** — la fila describe qué hay *ahora*; al cambiar el equipo se
  edita la fila. Simple, y es lo que hace hoy `proyecto_inversores`. Se pierde la
  historia: las fallas del equipo viejo quedan colgando del nuevo y «¿qué marca nos da
  más problemas?» empieza a mentir en cuanto haya reemplazos.
- **Registro con historia** — el equipo que sale se marca retirado con su fecha y entra
  una fila nueva. Habilita garantía, MTBF y análisis por marca de verdad. Cuesta que
  toda consulta filtre por vigencia.

**Recomendación:** historia para los equipos serializados y caros; estado simple para
los de conjunto. **Sin decidir.**

### 7.2 El detector de alarmas sin interfaz

El detector de desconexión sigue corriendo cada 15 minutos y escribiendo en
`alarmas_monitoreo`, pero al retirar `/alertas/monitoreo` ya no hay forma de resolver
una alarma desde la web. El contador del Dashboard sube y no baja. Hay que decidir si
se apaga el detector o si su resolución se reubica en `/fallas`.

### 7.3 `proyecto_inicio_operacion` sin quien la escriba

Al retirar la vista Inicio de Operación desapareció el único formulario que escribía
en esa tabla. El Informe de Puesta en Marcha sigue leyendo de ahí cinco campos
(checklist, fecha de energización, fecha de puesta en marcha, contratista,
pendientes). Las plantas con ficha la conservan; una planta nueva no puede recibir una.

Arreglo barato, dos caminos: volver a montar solo el `PUT /inicio-operacion/{id}`, o
mover esos cinco campos al formulario del Informe O&M.

---

## 8. Cómo reproducir esto

### Snapshot del esquema de producción

Vive **fuera de este repo**, en la raíz del workspace de trabajo, en
`esquema-bd-produccion/`:

- `esquema.json` — columnas, tipos, PK, FK, índices, uniques de las 125 tablas.
- `esquema_produccion.sql` — el DDL completo.
- `ESQUEMA_BD_PRODUCCION.md`, `DEPURACION.md` — lecturas ya redactadas.

> **Advertencia.** Los documentos derivados se equivocan. `DEPURACION.md` listaba
> `mantenimiento_impacto.falla_id` entre las FK faltantes y **era incorrecto**: el DDL sí
> la declaraba. Esa tabla se eliminó el 2026-09-07, pero el criterio queda: ante la duda
> gana `esquema_produccion.sql`, no la prosa.

### Verificar el código antes de creerle a un documento

El repo local puede estar decenas de commits atrás de lo que corre en producción.
Antes de analizar nada:

```bash
git fetch origin && git rev-list --left-right --count master...origin/master
```

Si el segundo número no es 0, el local está atrasado.

### Repos y despliegue

- Frontend: `unergy-operaciones-frontend` → Vercel (auto-deploy desde `master`).
- Backend: `unergy-operaciones-backend` → Railway (auto-deploy desde `master`).
- Ambos migraron de la organización `sole-open-source` a **`klima-open-source`**; los
  remotes locales todavía apuntan a la vieja y funcionan por redirección de GitHub.

> El hash del bundle que compila Vercel **no coincide** con el de un `npm run build`
> local. Para verificar si un cambio ya está en producción, descarga el bundle y busca
> la ruta, no compares hashes.

---

## 9. Documentos visuales que acompañan esto

| Documento | Qué contiene |
|---|---|
| Radiografía de Operaciones | Diagnóstico de las tres vistas: fuentes, lógica y los 23 hallazgos con diagramas de mecanismo. |
| Mapa de Operaciones | ERD de las 25 tablas con las 34 FK reales y los 2 cruces por texto. |
| Esquema de Operaciones | Las 24 tablas columna por columna: tipo, nulabilidad, defaults, claves. |
| Anatomía del Inventario | Las cuatro representaciones del equipo y qué preguntas no puede responder el modelo. |

Los enlaces están en la carpeta de artefactos de Claude Code (`/artifacts`).

> Los cuatro son **anteriores al 2026-09-07**, cuando se eliminó `mantenimiento_impacto`
> (§5.8). Sus conteos —25 tablas, 34 FK— describen el esquema de entonces, y por eso se
> dejan como estaban: son la leyenda de un diagrama que todavía dice eso.

---

## 10. Mantenimiento de este documento

Este archivo es la **copia canónica**. Existe un puntero en el workspace de trabajo en
`contexto_para_dumies/bakcend_contexto/`; si editas, edita aquí.

Está fechado: describe el estado al **23 de agosto de 2026**. Antes de confiar en una
línea concreta, verifica contra el código — el §8 explica cómo. Si encuentras que algo
cambió, actualiza el documento en el mismo commit que hizo el cambio.
