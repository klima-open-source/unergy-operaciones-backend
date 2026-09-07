# API de Fallas — Plataforma Operaciones Unergy

Guía para crear y consultar fallas de forma programática.

- **Base URL:** `https://backend-production-63d8.up.railway.app`
- **Prefijo:** todos los endpoints viven bajo `/api/v1`
- **Swagger interactivo:** https://backend-production-63d8.up.railway.app/docs
- **Formato:** JSON en request y response (`Content-Type: application/json`), salvo la subida de archivos

---

## ⚠️ Antes de empezar: esto es producción

**No hay entorno de staging.** Este backend es el que usa el equipo de operaciones todos los días. Cada falla que creen por API es una falla real, con estos efectos:

| Efecto | Cuándo ocurre | Qué pasa |
|---|---|---|
| **Notificación a coordinadores** | En **toda** creación de falla, sin excepción | Se crea una notificación in-app para cada usuario con rol `coordinador` activo |
| **Alarmas de comunicación** | Si mandan `frontera_perdida_comunicacion: true`, o un inversor con el tipo `perdida_comunicacion` | Notificaciones a los roles `admin`, `operaciones` y `monitoreo`. Si en el mismo proyecto coinciden pérdida de frontera **y** de inversores, se genera la alarma crítica `comunicacion_total`. Se reevalúa tanto en `POST` como en `PATCH` |
| **Aparece en las vistas de operación** | Siempre | La falla sale en `/fallas`, `/operaciones/gestion-fallas`, la app móvil y el resumen del día |
| **Consume un código real** | Siempre | Los `FAL-2026-#####` son una secuencia compartida |
| **📧 Correo a clientes reales** | **Solo** si llaman `POST /api/v1/fallas/{id}/notificar` | Envía correo a los contactos operacionales del cliente dueño del proyecto |

**Las dos reglas que les pedimos:**

1. **No llamen `POST /fallas/{id}/notificar`.** Ese es el único endpoint que le manda correo a un cliente externo. El campo `notificacion: true` en el payload **no** envía nada por sí solo — es solo una bandera que queda guardada en la fila. El correo sale exclusivamente con esa llamada explícita.
2. **Marquen sus fallas de prueba con un prefijo reconocible en `descripcion`** (p. ej. `"[PRUEBA API] ..."`). El campo `centinela` que se usaba para esto se eliminó (2026-09-02) — era texto libre sin validación, cualquiera podía escribir `"MGS_AUTO"` sin serlo, así que dejó de ser una señal confiable. Con el prefijo pueden encontrar sus pruebas después vía `GET /fallas?q=PRUEBA API` y borrarlas.

Para borrar lo que creen: `DELETE /api/v1/fallas/{id}` (es borrado lógico, marca `deleted_at`).

---

## 1. Autenticación

Todos los endpoints exigen autenticación. Hay dos formas; **usen la API Key**.

### API Key (recomendado para scripts)

Header `X-API-Key` en cada request:

```bash
curl https://backend-production-63d8.up.railway.app/api/v1/fallas/catalogos \
  -H "X-API-Key: uop_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

La key tiene formato `uop_` + 64 caracteres hex. **Pídansela a Juan José** — se emite desde Admin → Usuarios → API Keys y solo se muestra una vez al crearla.

Verifiquen que funciona:

```bash
curl https://backend-production-63d8.up.railway.app/api/v1/api-keys/verify \
  -H "X-API-Key: $UNERGY_API_KEY"
# → {"user_id": 3, "nombre": "...", "email": "...", "rol": "operaciones"}
```

> **Ojo con los permisos:** la API Key hereda **todos** los permisos del usuario al que está asociada. El campo `scopes` se guarda pero hoy no se valida. Trátenla como una contraseña: variable de entorno, nunca en el repo, nunca en un frontend.

### Bearer token (alternativa)

```bash
curl -X POST https://backend-production-63d8.up.railway.app/api/v1/auth/token \
  -d "username=correo@unergy.io&password=..."
# → {"access_token": "eyJ..."}
```

Luego `Authorization: Bearer eyJ...`. El body es `application/x-www-form-urlencoded`, no JSON.

### Errores de autenticación

| Código | Detalle | Causa |
|---|---|---|
| 401 | `API Key inválida` | La key no existe o está desactivada |
| 401 | `Usuario inactivo o no encontrado` | La key es válida pero su usuario fue desactivado |
| 401 | `Token requerido` | No mandaron ni `X-API-Key` ni `Authorization` |

---

## 2. Quickstart — crear una falla en 3 llamadas

### Paso 1: traer los catálogos (los IDs que van a necesitar)

```bash
curl https://backend-production-63d8.up.railway.app/api/v1/fallas/catalogos \
  -H "X-API-Key: $UNERGY_API_KEY"
```

```json
{
  "estados": [
    { "id": 1, "codigo": "abierta", "etiqueta": "Abierta",
      "orden": 1, "es_estado_final": false }
  ],
  "prioridades": [
    { "id": 4, "codigo": "critica", "etiqueta": "Crítica", "nivel": 4 }
  ],
  "tipos": [
    { "id": 12, "codigo": "red.baja_tension", "etiqueta": "Baja tensión",
      "descripcion": null, "categoria": { "id": 1, "codigo": "red", "etiqueta": "Red", "…": "…" } }
  ],
  "resoluciones": [
    { "id": 1, "codigo": "reparacion", "etiqueta": "Reparación" }
  ]
}
```

Los IDs **no son estables entre entornos** y los ejemplos de arriba son ilustrativos: siempre resuélvanlos leyendo este endpoint, no los hardcodeen. Códigos que sabemos que existen: estados `abierta` y `programado`; prioridades `critica` y `alta`. La lista completa la da el endpoint.

### Paso 2: elegir un proyecto

```bash
curl "https://backend-production-63d8.up.railway.app/api/v1/proyectos?page=1&size=50" \
  -H "X-API-Key: $UNERGY_API_KEY"
```

Devuelve `{ "items": [...], "total": …, "page": 1, "size": 50, … }`; el `size` admite hasta 500. Acepta además los filtros `q` (búsqueda por nombre), `estado`, `tipo_proyecto`, `portafolio_id` y `servicio`.

Necesitan el `id` del proyecto. Para pruebas, pídanle a Juan José que les indique uno seguro.

### Paso 3: crear la falla

```bash
curl -X POST https://backend-production-63d8.up.railway.app/api/v1/fallas \
  -H "X-API-Key: $UNERGY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "proyecto_id": 147,
    "estado_id": 1,
    "prioridad_id": 3,
    "descripcion": "[PRUEBA API] Prueba de integración — ignorar",
    "fecha_identificacion": "2026-07-28",
    "categoria_codigo": "red",
    "subtipo_codigo": "baja_tension"
  }'
```

Respuesta `201 Created` con la falla completa, incluido el `codigo_interno` que asigna el servidor:

```json
{
  "id": 5831,
  "codigo_interno": "FAL-2026-05831",
  "proyecto": { "id": 147, "nombre_comercial": "La Reserva", "…": "…" },
  "estado": { "id": 1, "codigo": "abierta", "etiqueta": "Abierta", "…": "…" },
  "clasificacion": {
    "categoria": "red",
    "categoria_etiqueta": "Red",
    "subtipo": "baja_tension",
    "subtipo_etiqueta": "Baja tensión"
  },
  "pendiente_reclasificar": false,
  "…": "…"
}
```

---

## 3. `POST /api/v1/fallas` — referencia completa de campos

31 campos, de los cuales **solo 5 son obligatorios**:

`proyecto_id` · `estado_id` · `prioridad_id` · `descripcion` · `fecha_identificacion`

Todo lo demás es opcional. Los campos que el servidor asigna solo (y que **no** deben mandar) son `codigo_interno`, `registrado_por_id`, `id`, `created_at`, `updated_at`.

### 3.1 Identificación — obligatorios

| Campo | Tipo | Req. | Descripción |
|---|---|:---:|---|
| `proyecto_id` | `int` | ✅ | ID del proyecto afectado. Debe existir (FK a `proyectos`) |
| `descripcion` | `string` | ✅ | Texto libre, sin límite de longitud. Es lo que se ve en los listados y en los correos |
| `fecha_identificacion` | `date` | ✅ | `"YYYY-MM-DD"`. Fecha en que se detectó la falla |
| `hora_identificacion` | `time` | — | `"HH:MM:SS"`. Complementa la fecha para el cálculo de tiempo de afectación |

### 3.2 Estado y asignación

| Campo | Tipo | Req. | Descripción |
|---|---|:---:|---|
| `estado_id` | `int` | ✅ | ID de `fallas_cat_estados`. Ver `GET /fallas/catalogos` |
| `prioridad_id` | `int` | ✅ | ID de `fallas_cat_prioridades`. Ver `GET /fallas/catalogos` |
| `resolucion_id` | `int` | — | ID de `fallas_cat_resoluciones`. Solo tiene sentido al cerrar |
| `sla_limite_horas` | `int` | — | Horas de SLA personalizadas para esta falla puntual, si se sale del default de su prioridad. Si no se manda, la respuesta calcula el default solo (crítica=8h, grave=24h, media=72h, leve=168h) y lo expone en `sla_limite_horas_efectivo` -- `sla_limite_horas` (este campo) queda `null` a menos que ustedes lo hayan mandado explícitamente. La respuesta también expone `sla_limite_dias` derivado del efectivo (división entera por 24) |
| `fecha_programada` | `date` | — | `"YYYY-MM-DD"`. Fecha de la intervención programada. Se usa con el estado `programado` |

### 3.3 Clasificación estructurada — recomendado

Es la metodología vigente: primero el **sistema afectado**, luego el detalle. Ver la sección 4 para los valores válidos.

| Campo | Tipo | Req. | Descripción |
|---|---|:---:|---|
| `categoria_codigo` | `string` | — | Sistema afectado: `red` \| `frontera` \| `inversores` \| `generando_sin_datos` \| `eventos_adversos`. **Manden esto siempre en fallas nuevas.** Si se omite, la falla queda sin clasificar |
| `subtipo_codigo` | `string` | condicional | **Obligatorio** en todas las categorías salvo `inversores`, que no lo usa |
| `subtipo_detalle` | `string` | — | Texto libre. La estructura lo marca como requerido para `red.mantenimiento_red` y `eventos_adversos.otro`, pero **el servidor no lo exige** (solo el formulario web). Mándenlo igual en esos dos casos |
| `frontera_afecta_medicion` | `bool` | — | Solo `frontera`. Si la falla afecta la medición comercial. En otras categorías se fuerza a `null` |
| `frontera_perdida_comunicacion` | `bool` | — | Solo `frontera`. ⚠️ **`true` dispara la alarma `comunicacion_frontera`** |
| `inversores` | `array` | condicional | **Obligatorio** si `categoria_codigo` es `inversores`. Ver 3.4 |

### 3.4 Objeto `inversores[]`

Solo con `categoria_codigo: "inversores"`. Una entrada por inversor afectado.

| Campo | Tipo | Req. | Descripción |
|---|---|:---:|---|
| `proyecto_inversor_id` | `int` | — | ID del inversor. Tráiganlo de `GET /api/v1/proyectos/{id}/inversores` |
| `nombre` | `string` | — | Nombre del inversor. Se guarda como snapshot para que el histórico no cambie si luego renombran el inversor |
| `potencia_kw` | `float` | — | Potencia del inversor. También snapshot |
| `tipos` | `string[]` | ✅ | Tipos de falla de este inversor. **Al menos uno en total** entre todos los inversores del array, y todos deben ser códigos válidos (ver 4.3) |

⚠️ Si alguno de los `tipos` es `perdida_comunicacion`, se dispara la alarma `comunicacion_inversores`.

> La estructura marca `requiere_proyecto_unico: true` para esta categoría: una falla de inversores pertenece a un solo proyecto.

### 3.5 Clasificación legada — no usar en fallas nuevas

| Campo | Tipo | Req. | Descripción |
|---|---|:---:|---|
| `tipo_id` | `int` | — | ID de `fallas_cat_tipos`. Esquema plano anterior |

> **Importante:** si mandan `categoria_codigo`, el servidor **sobrescribe** `tipo_id` con el valor derivado de la clasificación. No intenten controlarlo manualmente en el camino estructurado — se pierde lo que manden. (Esto es a propósito: evita títulos que contradigan la clasificación.) `tipo_libre` existió como campo plano hasta 2026-09-02; se eliminó — el campo `"tipo"` en las respuestas de lectura ahora se arma siempre a partir de `clasificacion` (o de `tipo_id` para fallas legacy sin clasificación).

### 3.6 Ventana temporal

| Campo | Tipo | Req. | Descripción |
|---|---|:---:|---|
| `fecha_ocurrencia` | `datetime` | — | ISO 8601, `"2026-07-28T14:30:00-05:00"`. Inicio real de la falla. Si se omite, el cálculo usa `fecha_identificacion` + `hora_identificacion` |
| `fecha_resolucion` | `datetime` | — | ISO 8601. Cierre de la falla |
| `intervalos` | `array` | — | Intervalos de disparo, para fallas intermitentes. Ver abajo |

**Objeto `intervalos[]`:**

| Campo | Tipo | Req. | Descripción |
|---|---|:---:|---|
| `inicio` | `datetime` | ✅ | ISO 8601 |
| `fin` | `datetime` | — | ISO 8601. `null` = intervalo aún abierto |
| `nota` | `string` | — | Texto libre |

> Cómo se calcula `tiempo_afectacion_horas` en la respuesta: **si hay intervalos**, suma la duración de cada uno (los abiertos se cierran provisionalmente con la hora actual). **Si no hay intervalos**, usa el span único `fecha_ocurrencia` → `fecha_resolucion`. Si la falla está abierta y no tiene intervalos, devuelve `null`. Las fechas sin zona horaria se interpretan como Colombia (UTC-5).

### 3.7 Impacto y cierre

| Campo | Tipo | Req. | Descripción |
|---|---|:---:|---|
| `kwh_perdidos_estimado` | `float` | — | Energía no generada. Numérico con 3 decimales |
| `impacto_economico_cop` | `float` | — | Impacto en pesos. Numérico con 2 decimales |
| `causa_raiz` | `string` | — | Texto libre |
| `acciones_correctivas` | `string` | — | Texto libre |
| `generar_impacto` | `bool` | — | Default `false`. ⚠️ Si es `true`, además de la falla **crea una fila en `mantenimiento_impacto`** con la energía perdida calculada sobre la ventana `[fecha_ocurrencia, fecha_resolucion]`. Déjenlo en `false` para pruebas |

### 3.8 Metadatos y trazabilidad

| Campo | Tipo | Req. | Descripción |
|---|---|:---:|---|
| `alarma_monitoreo_id` | `int` | — | ID de la alarma de monitoreo que originó la falla, con FK real hacia `alarmas_monitoreo`. Déjenlo vacío — es de uso interno del motor MGS |
| `notificacion` | `bool` | — | Default `false`. **Bandera únicamente — no envía ningún correo.** El correo sale solo con `POST /fallas/{id}/notificar` |
| `fotos_urls` | `string[]` | — | Lista de URLs. Para subir archivos de verdad usen `POST /fallas/{id}/archivos` |

---

## 4. Valores válidos de la clasificación

Fuente de verdad en vivo: `GET /api/v1/fallas/estructura`. Lo de abajo es su contenido al 28 de julio de 2026.

```bash
curl https://backend-production-63d8.up.railway.app/api/v1/fallas/estructura \
  -H "X-API-Key: $UNERGY_API_KEY"
# → {"categorias": [ ... ]}
```

### 4.1 `red` — "Red"

Eventos del suministro eléctrico externo al proyecto. Requiere `subtipo_codigo`.

| `subtipo_codigo` | Etiqueta | Notas |
|---|---|---|
| `baja_tension` | Baja tensión | |
| `alta_tension` | Alta tensión | |
| `variacion_frecuencia` | Variación de frecuencia | |
| `mantenimiento_red` | Mantenimiento de red | Manden `subtipo_detalle` con el motivo (árbol sobre la línea, fusible disparado, mant. programado, cambio de poste…) |
| `acometida_mt` | Acometida en media tensión | |
| `transformador` | Transformador | |
| `desconexion_sin_identificar` | Desconexión sin identificar | Estado temporal: el servidor marca la falla con `pendiente_reclasificar: true` hasta que se reclasifique con la causa definitiva |

### 4.2 `frontera` — "Frontera"

Equipos de la medición comercial. Requiere `subtipo_codigo`. Acepta los dos flags `frontera_*`.

| `subtipo_codigo` | Etiqueta |
|---|---|
| `medidor_principal` | Medidor principal |
| `medidor_respaldo` | Medidor de respaldo |
| `ct` | CT (Transformadores de corriente) |
| `pt` | PT (Transformadores de potencial) |
| `caja_pruebas` | Caja de pruebas / Hornera |
| `modem_comunicaciones` | Módem de comunicaciones |

### 4.3 `inversores` — "Inversores"

**No** usa `subtipo_codigo`. Usa `inversores[]`, y cada entrada lleva uno o más de estos códigos en `tipos`:

| Código | Etiqueta |
|---|---|
| `baja_tension_ac` | Baja tensión AC |
| `baja_tension_dc` | Baja tensión DC |
| `baja_resistencia_aislamiento` | Baja resistencia de aislamiento |
| `problemas_ventilacion` | Problemas de ventilación |
| `falla_dispositivo` | Falla del dispositivo |
| `problema_cadena_fotovoltaica` | Problema en cadena fotovoltaico |
| `sobre_temperatura` | Sobre temperatura |
| `arco_ac` | Arco en AC |
| `arco_dc` | Arco en DC |
| `perdida_comunicacion` | Pérdida de comunicación (internet) — ⚠️ dispara alarma |

> Estos cuatro códigos fueron **retirados** y ya no se aceptan al crear, aunque siguen resolviéndose para no degradar fallas históricas: `no_generacion`, `generacion_anomala`, `limitacion_potencia`, `strings_mal_conectados`. Si los mandan, reciben 422.

### 4.4 `generando_sin_datos` — "Sistema generando pero sin datos"

No llegan datos de generación y desde el monitoreo no se puede afirmar si la planta generó o no. Requiere `subtipo_codigo`: el subtipo **no es un evento, es el resultado de la verificación en sitio**.

| `subtipo_codigo` | Etiqueta | Notas |
|---|---|---|
| `incertidumbre` | Incertidumbre (no sabemos si generó o no) | Aún sin verificar en sitio: el servidor marca la falla con `pendiente_reclasificar: true` hasta que se confirme encendido/apagado |
| `verificado_encendido` | Verificado en sitio: proyecto encendido | La planta sí genera; la falla es solo de datos/monitoreo |
| `verificado_apagado` | Verificado en sitio: proyecto apagado | Se confirmó en sitio que no está generando |

> Flujo esperado: se abre con `incertidumbre` y, cuando alguien verifica en sitio, se hace `PATCH /fallas/{id}` con el `subtipo_codigo` definitivo — eso limpia solo `pendiente_reclasificar`.

### 4.5 `eventos_adversos` — "Eventos naturales"

Requiere `subtipo_codigo`. Nótese que el código es `eventos_adversos` pero la etiqueta que se muestra es "Eventos naturales" — **búsquenlo por código, no por etiqueta**.

| `subtipo_codigo` | Etiqueta | Notas |
|---|---|---|
| `incendio` | Incendio | |
| `inundacion` | Inundación | |
| `huracan` | Huracán | |
| `clima_nublado_lluvioso` | Clima nublado/lluvioso | |
| `otro` | Otro | Manden `subtipo_detalle` describiendo el evento |

---

## 5. Ejemplos completos, uno por categoría

Todos asumen `proyecto_id: 147` y `estado_id: 1` (abierta); la prioridad varía según el ejemplo. Los IDs son ilustrativos — **resuelvan los suyos con `/catalogos`.**

### Red — con detalle

```json
{
  "proyecto_id": 147,
  "estado_id": 1,
  "prioridad_id": 3,
  "descripcion": "Corte de energía por mantenimiento del operador de red",
  "fecha_identificacion": "2026-07-28",
  "hora_identificacion": "08:15:00",
  "fecha_ocurrencia": "2026-07-28T08:10:00-05:00",
  "categoria_codigo": "red",
  "subtipo_codigo": "mantenimiento_red",
  "subtipo_detalle": "Poda de árbol sobre la línea de MT"
}
```

### Frontera — con pérdida de comunicación

⚠️ Este ejemplo **dispara la alarma `comunicacion_frontera`**. Si no quieren generar alarmas, pongan los dos flags en `false`.

```json
{
  "proyecto_id": 147,
  "estado_id": 1,
  "prioridad_id": 4,
  "descripcion": "Módem de la frontera sin reportar datos desde las 02:00",
  "fecha_identificacion": "2026-07-28",
  "categoria_codigo": "frontera",
  "subtipo_codigo": "modem_comunicaciones",
  "frontera_afecta_medicion": false,
  "frontera_perdida_comunicacion": true
}
```

### Inversores — dos inversores afectados

```json
{
  "proyecto_id": 147,
  "estado_id": 1,
  "prioridad_id": 3,
  "descripcion": "Inversores 1 y 2 con sobre temperatura y ventilación obstruida",
  "fecha_identificacion": "2026-07-28",
  "categoria_codigo": "inversores",
  "inversores": [
    {
      "proyecto_inversor_id": 512,
      "nombre": "Inversor 1",
      "potencia_kw": 300,
      "tipos": ["sobre_temperatura", "problemas_ventilacion"]
    },
    {
      "proyecto_inversor_id": 513,
      "nombre": "Inversor 2",
      "potencia_kw": 300,
      "tipos": ["sobre_temperatura"]
    }
  ]
}
```

### Evento natural — falla ya resuelta, con intervalos

```json
{
  "proyecto_id": 147,
  "estado_id": 1,
  "prioridad_id": 2,
  "descripcion": "Generación reducida por cielo cubierto durante la mañana",
  "fecha_identificacion": "2026-07-27",
  "fecha_ocurrencia": "2026-07-27T06:00:00-05:00",
  "fecha_resolucion": "2026-07-27T11:30:00-05:00",
  "categoria_codigo": "eventos_adversos",
  "subtipo_codigo": "clima_nublado_lluvioso",
  "kwh_perdidos_estimado": 842.5,
  "intervalos": [
    { "inicio": "2026-07-27T06:00:00-05:00", "fin": "2026-07-27T09:00:00-05:00", "nota": "Nubosidad alta" },
    { "inicio": "2026-07-27T10:15:00-05:00", "fin": "2026-07-27T11:30:00-05:00", "nota": "Lluvia" }
  ]
}
```

---

## 6. Qué deriva el servidor automáticamente

Cuando mandan `categoria_codigo`, el servidor calcula y sobrescribe estos campos. No los manden:

| Campo derivado | Cómo se calcula |
|---|---|
| `codigo_interno` | `FAL-{año}-{id:05d}`, con el `id` autoincremental. Siempre |
| `registrado_por_id` | El usuario dueño de la API Key. No se puede suplantar |
| `tipo_id` | Se busca el tipo de catálogo con código `"{categoria}.{subtipo}"`. En `inversores` usa el primer tipo alfabéticamente. Si no existe, queda `null` — solo sirve como compatibilidad con vistas legacy, ya no arma el título |
| `clasificacion` | Snapshot JSON de la clasificación (categoría, subtipo, etiquetas, flags, e inversores con sus etiquetas). Es la única fuente que usan las vistas y el campo `"tipo"` de las respuestas para armar el título — reemplaza a `tipo_libre` (eliminado 2026-09-02) |
| `pendiente_reclasificar` | `true` solo si el subtipo lo marca (hoy: `red.desconexion_sin_identificar`) |
| `inversores_perdida_comunicacion` | `true` si algún inversor trae el tipo `perdida_comunicacion`. `null` si la categoría no es `inversores` |
| `frontera_afecta_medicion`, `frontera_perdida_comunicacion` | Se fuerzan a `null` si la categoría **no** es `frontera` |

---

## 7. Consultar las fallas creadas

### `GET /api/v1/fallas/por-proyecto` — **RETIRADO** (2026-09-07)

Este endpoint devolvia las fallas de una planta agrupadas en tres estados
(`vigente` / `programado` / `terminado`). **Se elimino**: no lo consumia
ningun flujo interno ni el frontend, y no habia evidencia de integracion
activa contra el.

Si necesitan consultar fallas, usen **`GET /api/v1/fallas`** (documentado mas
abajo): trae los mismos datos con el catalogo de estados completo. Y si
hacen falta las tres cubetas otra vez, escribannos y lo reponemos -- esta en
el historial de git, no se perdio.

### `GET /api/v1/fallas` — listado paginado

```bash
curl "https://backend-production-63d8.up.railway.app/api/v1/fallas?page=1&size=20&proyecto_id=147" \
  -H "X-API-Key: $UNERGY_API_KEY"
```

Respuesta: `{ "items": [...], "total": 137, "page": 1, "size": 20, "pages": 7 }`. Orden fijo: `created_at` descendente. Las fallas borradas (`deleted_at`) quedan excluidas.

**Filtros disponibles** (todos opcionales, se combinan con AND):

| Parámetro | Tipo | Descripción |
|---|---|---|
| `page` | `int` ≥1 | Default 1 |
| `size` | `int` 1–5000 | Default 20. `page_size` es un alias que tiene precedencia |
| `q` / `buscar` | `string` | Búsqueda parcial, insensible a mayúsculas, sobre `descripcion` **o** `codigo_interno` |
| `estado_id` / `estado_codigo` | `int` / `string` | Por ID o por código (`abierta`, …) |
| `prioridad_id` / `prioridad_codigo` | `int` / `string` | Por ID o por código |
| `tipo_codigo` | `string` | Código del tipo de catálogo, p. ej. `red.baja_tension` |
| `proyecto_id` | `int` | |
| `cliente_id` | `int` | Fallas de los proyectos donde ese cliente es inversionista vigente |
| `solo_alerta` | `bool` | Solo fallas no cerradas identificadas hace más de 7 días |
| `fecha_programada_desde` / `_hasta` | `date` | Rango sobre `fecha_programada` |
| `con_fecha_programada` | `bool` | Solo las que tienen `fecha_programada` |
| `pendiente_reclasificar` | `bool` | Con `true`: solo las que de verdad necesitan que alguien confirme la causa a mano — excluye automáticamente el patrón de reportes de un integrador externo que quedan atascados en el subtipo genérico "sin identificar" sin `alarma_monitoreo_id` (2026-09-02) |

> Para encontrar sus fallas de prueba, usen `?q=` con el prefijo que le hayan puesto a `descripcion` (`centinela` ya no existe, ver sección 2).

### `GET /api/v1/fallas/{id}` — detalle

Devuelve el mismo objeto que el listado. Incluye `seguimientos[]`, `intervalos[]` e `inversores_afectados[]` completos, más los derivados `dias_abierta`, `tiempo_afectacion_horas` y `sla_limite_dias`. `404` si no existe.

### Otros endpoints de lectura

| Endpoint | Qué devuelve |
|---|---|
| `GET /api/v1/fallas/catalogos` | Estados, prioridades, tipos activos y resoluciones |
| `GET /api/v1/fallas/estructura` | La jerarquía de clasificación (sección 4) |
| `GET /api/v1/fallas/stats/resumen` | Conteos agregados |
| `GET /api/v1/fallas/sla-dashboard` | Indicadores de SLA |
| `GET /api/v1/fallas/{id}/impacto` | Impacto estimado de la falla |
| `GET /api/v1/fallas/{id}/archivos` | Adjuntos de la falla |
| `GET /api/v1/fallas/actividad-hoy` | Fallas creadas o modificadas hoy |

---

## 8. Modificar fallas

### `PATCH /api/v1/fallas/{id}` — actualización parcial

Acepta el mismo conjunto de campos que el create (todos opcionales), más `pendiente_reclasificar`. **No** acepta `proyecto_id` ni `generar_impacto`. `sla_cumplido` tampoco se acepta -- es siempre calculado (se sella al pasar a un estado final, comparando `fecha_resolucion` contra `sla_limite_horas`/el default por prioridad).

Solo se aplican los campos presentes en el body. Mandar `categoria_codigo` recalcula toda la clasificación y, si el nuevo subtipo no es de los pendientes, limpia `pendiente_reclasificar` — así se reclasifica una desconexión sin identificar:

```bash
curl -X PATCH https://backend-production-63d8.up.railway.app/api/v1/fallas/5831 \
  -H "X-API-Key: $UNERGY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"categoria_codigo": "red", "subtipo_codigo": "transformador"}'
```

> Si mandan `inversores`, **reemplaza** la lista completa de inversores afectados (no hace merge). Si omiten el campo, los inversores existentes quedan intactos y la clasificación se recalcula a partir de ellos.

**Dos efectos automáticos del `PATCH` que conviene tener presentes:**

1. **Sellado de `fecha_resolucion`.** Si cambian `estado_id` a un estado con `es_estado_final: true` y la falla no tiene `fecha_resolucion`, el servidor la pone en el instante actual. Y si la mueven a un estado **no** final sin mandar `fecha_resolucion` explícitamente, el servidor la **borra** (`null`). Si necesitan controlar esa fecha, mándenla en el mismo request.

La recalculación de la clasificación solo ocurre si tocan alguno de `categoria_codigo`, `subtipo_codigo`, `subtipo_detalle`, `frontera_afecta_medicion`, `frontera_perdida_comunicacion` o `inversores`, **y** la falla tiene `categoria_codigo`. Un `PATCH` que solo cambia, por ejemplo, `descripcion` no toca nada de la clasificación.

### `POST /api/v1/fallas/{id}/seguimientos` — agregar nota o cambiar estado

```json
{ "nota": "Se verificó en sitio", "estado_nuevo_id": 2 }
```

Ambos campos son opcionales. Devuelve `201`.

### `POST /api/v1/fallas/{id}/archivos` — subir adjunto

`multipart/form-data`, campo `archivo`. Límite **20 MB** (`400` si se excede). Los archivos van a Google Drive, en `Raíz → Proyecto → Código de falla`. Alias equivalente: `POST /api/v1/fallas/{id}/attachments`.

```bash
curl -X POST https://backend-production-63d8.up.railway.app/api/v1/fallas/5831/archivos \
  -H "X-API-Key: $UNERGY_API_KEY" \
  -F "archivo=@evidencia.jpg"
```

### `DELETE /api/v1/fallas/{id}` — borrado lógico

Devuelve `204`. Marca `deleted_at`; la falla desaparece de los listados pero la fila permanece. **Úsenlo para limpiar sus pruebas.**

---

## 9. Errores

| Código | Cuándo | Cuerpo |
|---|---|---|
| `401` | Autenticación inválida o ausente | `{"detail": "API Key inválida"}` |
| `404` | La falla del path no existe | `{"detail": "Falla no encontrada"}` |
| `422` | Validación de Pydantic: falta un campo obligatorio, tipo incorrecto, fecha malformada | Lista de errores con `loc` señalando el campo |
| `422` | Clasificación estructurada inválida | `{"detail": "Clasificación inválida: <razón>"}` |
| `400` | Archivo mayor a 20 MB | `{"detail": "El archivo supera el límite de 20 MB"}` |

Razones de "Clasificación inválida" que van a ver:

- `categoria_codigo requerido`
- `categoría desconocida: <valor>`
- `subtipo_codigo requerido` — categoría de opción/equipo sin subtipo
- `opción inválida '<subtipo>' para '<categoria>'`
- `debe indicar al menos un tipo de falla de inversor`
- `tipos de falla de inversor inválidos: [...]`

### IDs inexistentes: 422, no 500

`proyecto_id`, `estado_id`, `prioridad_id` y `resolucion_id` son foreign keys. Si mandan un ID que no existe, la API responde **`422`** con un mensaje indicando cuál de esos campos falló (antes del 2026-09-02 esto volaba como un `500` crudo de Postgres — ya corregido).

---

## 10. Ejemplo de integración en Python

```python
import os
import requests

BASE = "https://backend-production-63d8.up.railway.app/api/v1"
SESSION = requests.Session()
SESSION.headers["X-API-Key"] = os.environ["UNERGY_API_KEY"]


def catalogos():
    r = SESSION.get(f"{BASE}/fallas/catalogos", timeout=30)
    r.raise_for_status()
    return r.json()


def crear_falla(payload: dict) -> dict:
    # Marcamos siempre la descripción para que operaciones distinga las pruebas.
    payload["descripcion"] = f"[PRUEBA API] {payload.get('descripcion', '')}"
    r = SESSION.post(f"{BASE}/fallas", json=payload, timeout=30)
    if r.status_code == 422:
        raise ValueError(f"Payload inválido o ID inexistente: {r.json()['detail']}")
    r.raise_for_status()
    return r.json()


def main():
    cat = catalogos()
    estado = next(e for e in cat["estados"] if e["codigo"] == "abierta")
    prioridad = next(p for p in cat["prioridades"] if p["codigo"] == "alta")

    falla = crear_falla({
        "proyecto_id": 147,
        "estado_id": estado["id"],
        "prioridad_id": prioridad["id"],
        "descripcion": "Prueba de integración API — ignorar",
        "fecha_identificacion": "2026-07-28",
        "categoria_codigo": "red",
        "subtipo_codigo": "baja_tension",
    })
    print(falla["codigo_interno"], "→", falla["clasificacion"])

    # Limpieza
    SESSION.delete(f"{BASE}/fallas/{falla['id']}", timeout=30).raise_for_status()


if __name__ == "__main__":
    main()
```

---

## Resumen de lo que no deben hacer

- ❌ Llamar `POST /fallas/{id}/notificar` — le manda correo a clientes reales
- ❌ Hardcodear `estado_id` / `prioridad_id` / `tipo_id` — resuélvanlos desde `/catalogos`
- ❌ Mandar `tipo_id` junto con `categoria_codigo` — se sobrescribe
- ❌ Poner `generar_impacto: true` en pruebas — crea registros de mantenimiento
- ❌ Poner los flags `perdida_comunicacion` en `true` sin querer generar alarmas
- ❌ Exponer la API Key en un frontend o commitearla

Dudas o para pedir la key: **Juan José** — juanjose@unergy.io
