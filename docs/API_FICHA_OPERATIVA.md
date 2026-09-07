# API de la Ficha Operativa — CRM Comercial

Guía para consultar, por cada oferta comercial, los seis parámetros que pidió el equipo:
**nombre del proyecto, lugar, operador de red, energía real, energía promedio, fecha de
inicio de operación y tiempo del contrato de compra de energía.**

- **Base URL:** `https://operaciones.unergy.io`
- **Prefijo:** todos los endpoints viven bajo `/api/v1`
- **Sin Swagger interactivo.** La documentacion automatica la servia FastAPI en `/docs`, y desapareció con la migración a Django (2026-09-04). Esta guía es la referencia.
- **Formato:** JSON en request y response (`Content-Type: application/json`)

---

## ⚠️ Léanlo antes de la primera llamada

### 1. Esto todavía no está desplegado

La ficha está implementada y con tests, pero los commits siguen **locales**: no se han
pusheado ni desplegado. Hasta que Juan José confirme el deploy, `GET /comercial/ofertas`
responde **sin** el bloque `ficha` y los 4 campos declarados no existen.

**Pregúntenle a Juan José si ya está arriba antes de empezar.** Mientras tanto pueden ir
escribiendo el cliente contra la forma que documenta esta guía, que no va a cambiar.

### 2. Esto es producción, no hay staging

Es el mismo backend que usa operaciones todos los días. Leer (`GET`) es inofensivo. Crear y
editar **sí** deja datos reales en el CRM comercial:

| Acción | Efecto |
|---|---|
| `POST /comercial/oportunidades` | Crea un cliente en el pipeline, visible en `/comercial` |
| `POST /comercial/oportunidades/{id}/ofertas` | Crea una oferta real y **consume un consecutivo** de la secuencia compartida `OP.{SEG} No.{NNNN}-{MM}-{AAAA}` |
| `POST /comercial/ofertas/{id}/firmar` | **Crea un contrato PPA** con sus tarifas mensuales. Lo leen Cumplimiento y Liquidaciones |

**Las tres reglas que les pedimos:**

1. **Para probar solo lectura, no creen nada.** Lean las ofertas que ya existen.
2. Si necesitan crear, **usen un nombre de cliente reconocible** — algo como
   `PRUEBA API - <su nombre>` — para que operaciones sepa de inmediato que no es un negocio
   real y ustedes puedan encontrarlo después.
3. **No llamen `/firmar` sobre una oferta que no sea suya.** Crea un contrato y mueve la
   oferta a `firmado`; deshacerlo es manual.

Para limpiar lo que creen:

| Endpoint | Tipo de borrado |
|---|---|
| `DELETE /api/v1/comercial/ofertas/{id}` | **Duro — no se deshace.** No marca `deleted_at`, borra la fila |
| `DELETE /api/v1/comercial/oportunidades/{id}` | Lógico (marca `deleted_at`). **Solo rol `admin`** |

---

## 1. Autenticación

Igual que en la [API de Fallas](API_FALLAS.md): header `X-API-Key` en cada request.

```bash
export UNERGY_API_KEY="uop_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export BASE="https://operaciones.unergy.io/api/v1"

curl "$BASE/api-keys/verify" -H "X-API-Key: $UNERGY_API_KEY"
# → {"user_id": 3, "nombre": "...", "rol": "comercial"}
```

**Su usuario necesita rol `admin` o `comercial`.** Con cualquier otro rol todos los
endpoints de esta guía responden **403 `Requiere rol comercial o admin`**. Si `verify` les
devuelve `operaciones` o `monitoreo`, pídanle a Juan José una key con el rol correcto.

La key hereda todos los permisos de su usuario: trátenla como contraseña, en variable de
entorno, nunca en el repo ni en un frontend.

---

## 2. Quickstart — la ficha en una llamada

```bash
curl "$BASE/comercial/ofertas" -H "X-API-Key: $UNERGY_API_KEY"
```

Devuelve un **array plano**: una entrada por oferta. Cada una trae sus campos propios más el
bloque `ficha` con los seis parámetros ya resueltos.

Para traer una sola y mirarla cómoda:

```bash
curl "$BASE/comercial/ofertas?q=Catedral" -H "X-API-Key: $UNERGY_API_KEY" | jq '.[0].ficha'
```

---

## 3. El bloque `ficha`

```json
{
  "proyecto_nombre": "GD Catedral",
  "municipio": "Corozal",
  "departamento": "Sucre",
  "operador_red": "AFINIA S.A.S. E.S.P.",
  "operador_red_id": 1,
  "energia_promedio_kwh_mes": 178400.0,
  "energia_real_kwh_mes": 182351.3,
  "energia_real_periodo": "2026-07",
  "fecha_inicio_operacion": "2026-02-12",
  "contrato_compra_meses": 83,
  "contrato_compra_anios": 6.9,
  "contrato_fecha_inicio": "2026-02-12",
  "contrato_fecha_fin": "2032-12-31",
  "fuentes": {
    "proyecto_nombre": "proyecto",
    "municipio": "proyecto",
    "departamento": "proyecto",
    "operador_red": "proyecto",
    "energia_promedio_kwh_mes": "proyecto",
    "energia_real_kwh_mes": "generacion",
    "fecha_inicio_operacion": "contrato",
    "contrato_compra_meses": "contrato"
  }
}
```

### Referencia de campos

| Campo | Tipo | Qué es |
|---|---|---|
| `proyecto_nombre` | `string \| null` | Nombre de la planta |
| `municipio` | `string \| null` | Municipio |
| `departamento` | `string \| null` | Departamento |
| `operador_red` | `string \| null` | **Nombre legal** del operador de red |
| `operador_red_id` | `int \| null` | Su id en el catálogo `operadores_red` |
| `energia_promedio_kwh_mes` | `float \| null` | Generación mensual **estimada**, en kWh/mes |
| `energia_real_kwh_mes` | `float \| null` | Generación **real** del último mes cerrado, en kWh |
| `energia_real_periodo` | `string \| null` | A qué mes corresponde la real (`"2026-07"`) |
| `fecha_inicio_operacion` | `date \| null` | Inicio de suministro (`YYYY-MM-DD`) |
| `contrato_compra_meses` | `int \| null` | Duración del PPA en meses calendario |
| `contrato_compra_anios` | `float \| null` | La misma duración en años, 1 decimal |
| `contrato_fecha_inicio` | `date \| null` | Inicio del PPA |
| `contrato_fecha_fin` | `date \| null` | Fin del PPA |
| `fuentes` | `object` | De dónde salió cada valor — ver abajo |

**Todas las energías están en kWh/mes.** El modelo interno de proyectos guarda MWh; la API
ya hace la conversión para que ustedes no la hagan.

### `fuentes` — el campo que hace usable todo lo demás

Hoy la mayoría de los valores llegan en `null` porque la información todavía no está
cargada. `fuentes` es lo que les permite distinguir **"no aplica"** de **"todavía no lo
sabemos"**, que sin él se ven idénticos:

| Valor | Significa |
|---|---|
| `"proyecto"` | Salió del Proyecto: la planta ya existe en la plataforma |
| `"oferta"` | Declarado a mano en la oferta (la planta aún no existe como Proyecto) |
| `"contrato"` | Salió del PPA firmado |
| `"estimada"` | Fecha tentativa — **la oferta no está firmada todavía** |
| `"generacion"` | Calculado de lecturas reales de generación |
| `null` | **Nadie lo aportó todavía.** No es un error |

`fuentes` siempre trae estas 8 llaves: `proyecto_nombre`, `municipio`, `departamento`,
`operador_red`, `energia_promedio_kwh_mes`, `energia_real_kwh_mes`,
`fecha_inicio_operacion`, `contrato_compra_meses`.

### Cómo se resuelve cada campo

La cascada es **por campo**, en este orden:

```
Proyecto  →  lo declarado en la oferta  →  null
```

Es por campo y no por entidad: si el Proyecto tiene municipio pero no departamento, el
municipio sale del Proyecto y el departamento de lo declarado en la oferta. `fuentes` lo
dice campo por campo.

Dos reglas propias:

- **`fecha_inicio_operacion`** sale del PPA (`fuente: "contrato"`). Si la oferta no está
  firmada, se usa la fecha tentativa declarada y se marca `"estimada"`. **No** es la fecha
  de entrada en operación de la planta ni el inicio de comercialización.
- **`energia_real_kwh_mes`** es la del **último mes cerrado** con al menos 28 días de
  lectura. El mes en curso nunca cuenta: tres días de agosto no son "la energía del mes".
  Por eso siempre viene acompañada de `energia_real_periodo`.

---

## 4. Los tres casos que se van a encontrar

### A. La planta existe como Proyecto y ya genera

```json
"ficha": {
  "proyecto_nombre": "GD Catedral",
  "municipio": "Corozal", "departamento": "Sucre",
  "operador_red": "AFINIA S.A.S. E.S.P.", "operador_red_id": 1,
  "energia_promedio_kwh_mes": 178400.0,
  "energia_real_kwh_mes": 182351.3, "energia_real_periodo": "2026-07",
  "fecha_inicio_operacion": "2026-02-12",
  "contrato_compra_meses": 83, "contrato_compra_anios": 6.9,
  "contrato_fecha_inicio": "2026-02-12", "contrato_fecha_fin": "2032-12-31",
  "fuentes": { "municipio": "proyecto", "energia_real_kwh_mes": "generacion",
               "fecha_inicio_operacion": "contrato", "…": "…" }
}
```

### B. La planta no existe todavía — todo declarado en la oferta

El caso de GD Rio Pamplonita y GD Las Margaritas 1: son negocios reales cuya planta aún no
está en la plataforma.

```json
"ficha": {
  "proyecto_nombre": "GD Rio Pamplonita",
  "municipio": "Cúcuta", "departamento": "Norte de Santander",
  "operador_red": "ESSA S.A. E.S.P.", "operador_red_id": 2,
  "energia_promedio_kwh_mes": 174000.0,
  "energia_real_kwh_mes": null, "energia_real_periodo": null,
  "fecha_inicio_operacion": "2027-01-01",
  "contrato_compra_meses": null, "contrato_compra_anios": null,
  "contrato_fecha_inicio": null, "contrato_fecha_fin": null,
  "fuentes": { "municipio": "oferta", "fecha_inicio_operacion": "estimada",
               "energia_real_kwh_mes": null, "contrato_compra_meses": null }
}
```

Ojo: `fecha_inicio_operacion` tiene valor pero su fuente es `"estimada"` — es la fecha
tentativa, no un contrato firmado.

### C. Oferta nueva, sin información

**Este es el caso más común hoy.** Todo en `null`, incluidas las 8 llaves de `fuentes`:

```json
"ficha": {
  "proyecto_nombre": null, "municipio": null, "departamento": null,
  "operador_red": null, "operador_red_id": null,
  "energia_promedio_kwh_mes": null,
  "energia_real_kwh_mes": null, "energia_real_periodo": null,
  "fecha_inicio_operacion": null,
  "contrato_compra_meses": null, "contrato_compra_anios": null,
  "contrato_fecha_inicio": null, "contrato_fecha_fin": null,
  "fuentes": { "proyecto_nombre": null, "municipio": null, "…": null }
}
```

**La forma nunca cambia.** `ficha` siempre está presente con las 14 llaves, y `fuentes`
siempre con sus 8. No hace falta programar defensivamente contra llaves ausentes — sí contra
valores `null`.

---

## 5. Endpoints

### Leer

| Método | Ruta | Devuelve |
|---|---|---|
| `GET` | `/comercial/ofertas` | **Array plano de todas las ofertas.** El principal |
| `GET` | `/comercial/oportunidades/{id}` | Detalle del cliente; las ofertas van en `ofertas[]` |
| `GET` | `/comercial/oportunidades/{id}/ofertas` | Solo las ofertas de ese cliente |
| `GET` | `/comercial/oportunidades` | Lista de clientes del pipeline (sin ficha: la ficha es de la oferta) |

Filtros de `GET /comercial/ofertas`, todos opcionales y combinables:

| Parámetro | Valores | Ejemplo |
|---|---|---|
| `tipo` | `compra_energia`, `servicios_operacionales`, `comunidad_energetica` | `?tipo=compra_energia` |
| `estado` | `oportunidad`, `oferta`, `contrato`, `firmado`, `operando`, `terminado`, `declinado` | `?estado=operando` |
| `resultado` | `pendiente`, `aceptado`, `declinado` | `?resultado=aceptado` |
| `q` | Texto libre: código, planta, cliente | `?q=Catedral` |
| `solo_alerta` | `true` / `false` | `?solo_alerta=true` |

```bash
# Todas las ofertas operando, con su ficha
curl "$BASE/comercial/ofertas?estado=operando" -H "X-API-Key: $UNERGY_API_KEY" \
  | jq '.[] | {codigo: .codigo_seguimiento, ficha}'
```

La respuesta **no está paginada**: devuelve todas las ofertas que pasen el filtro.

### Escribir los datos declarados

Solo tiene sentido cuando la planta **no** existe como Proyecto — si existe, manda el
Proyecto y lo que escriban aquí queda de respaldo.

```
POST  /comercial/oportunidades/{id}/ofertas     crear
PATCH /comercial/ofertas/{oferta_id}            editar
```

Los 4 campos escribibles:

| Campo | Tipo | Regla |
|---|---|---|
| `municipio` | `string` | máx. 100 caracteres |
| `departamento` | `string` | máx. 100 caracteres |
| `operador_red_id` | `int` | **debe existir** en el catálogo `operadores_red` |
| `energia_promedio_kwh_mes` | `float` | `>= 0`, en kWh/mes |

```bash
curl -X PATCH "$BASE/comercial/ofertas/123" \
  -H "X-API-Key: $UNERGY_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "municipio": "Cúcuta",
    "departamento": "Norte de Santander",
    "operador_red_id": 2,
    "energia_promedio_kwh_mes": 174000
  }'
```

`operador_red_id` es una llave foránea, no texto libre. Los ids salen del catálogo; si les
falta un operador, **no lo escriban como texto**: háblenlo con Juan José para agregarlo al
catálogo.

También pueden mandar la fecha estimada de inicio con `fecha_tentativa_inicio`
(`"YYYY-MM-DD"`), que es la que alimenta `fecha_inicio_operacion` mientras la oferta no esté
firmada.

---

## 6. Probar sin datos

La ficha se puede ejercitar de punta a punta aunque hoy casi todo esté en `null`. Cuatro
llamadas, sobre un cliente de prueba que después pueden borrar.

```bash
# 1) Cliente de prueba, con nombre reconocible
OP=$(curl -s -X POST "$BASE/comercial/oportunidades" \
  -H "X-API-Key: $UNERGY_API_KEY" -H "Content-Type: application/json" \
  -d '{"cliente_nuevo": {"razon_social_nombre": "PRUEBA API - <su nombre>",
       "contactos": [{"email": "pruebas@unergy.io", "nombre": "Pruebas"}]}}' | jq -r '.id')

# 2) Una oferta SIN nada: la ficha debe venir completa y toda en null
OF=$(curl -s -X POST "$BASE/comercial/oportunidades/$OP/ofertas" \
  -H "X-API-Key: $UNERGY_API_KEY" -H "Content-Type: application/json" \
  -d '{"tipo": "compra_energia", "planta_nombre": "Planta de prueba"}' | jq -r '.id')

curl -s "$BASE/comercial/oportunidades/$OP/ofertas" -H "X-API-Key: $UNERGY_API_KEY" \
  | jq '.[0].ficha'
# → proyecto_nombre: "Planta de prueba" (fuente "oferta"), el resto null

# 3) Declaran lugar y energía: la ficha los refleja y fuentes dice "oferta"
curl -s -X PATCH "$BASE/comercial/ofertas/$OF" \
  -H "X-API-Key: $UNERGY_API_KEY" -H "Content-Type: application/json" \
  -d '{"municipio": "Corozal", "departamento": "Sucre",
       "energia_promedio_kwh_mes": 150000,
       "fecha_tentativa_inicio": "2027-03-01"}'

curl -s "$BASE/comercial/oportunidades/$OP/ofertas" -H "X-API-Key: $UNERGY_API_KEY" \
  | jq '.[0].ficha | {municipio, energia_promedio_kwh_mes, fecha_inicio_operacion, fuentes}'
# → fuentes.municipio = "oferta", fuentes.fecha_inicio_operacion = "estimada"

# 4) Limpieza (borrado DURO de la oferta, no se deshace)
curl -X DELETE "$BASE/comercial/ofertas/$OF" -H "X-API-Key: $UNERGY_API_KEY"
```

La oportunidad de prueba queda en el pipeline; para borrarla hace falta rol `admin` —
díganle a Juan José cuál crearon.

Qué vale la pena verificar en su cliente:

- [ ] `ficha` **siempre** viene, con las 14 llaves, aunque estén todas en `null`
- [ ] `fuentes` siempre trae sus 8 llaves
- [ ] Su UI no rompe con `null` en cualquiera de los campos
- [ ] Distinguen `fuente: null` (no hay dato) de un dato real — no muestren "—" para ambos
- [ ] Con `fuentes.fecha_inicio_operacion == "estimada"`, la fecha se marca como estimada en pantalla
- [ ] `energia_real_kwh_mes` nunca se muestra sin su `energia_real_periodo` al lado

---

## 7. Errores

| Código | Cuándo |
|---|---|
| 401 | Falta la API Key, o es inválida / está desactivada |
| 403 | `Requiere rol comercial o admin` — su usuario tiene otro rol |
| 403 | `Solo admin puede eliminar oportunidades` |
| 404 | `Oferta no encontrada` / `Oportunidad no encontrada` |
| 422 | `operador_red_id no existe en el catálogo de operadores` |
| 422 | Tipo inválido en algún campo (`energia_promedio_kwh_mes` negativa, fecha mal formada…) |

---

## 8. Preguntas frecuentes

**¿Por qué casi todo viene en `null`?**
Porque la información todavía no está cargada. La estructura se construyó primero, a
propósito, para que ustedes puedan integrar ya y los datos aparezcan solos cuando el
cargador de los cierres corra.

**¿Puedo pedir solo la ficha, sin el resto de la oferta?**
No hay un endpoint dedicado. Usen `jq '.[].ficha'` o el equivalente en su cliente.

**¿`energia_promedio_kwh_mes` es lo mismo que la energía del contrato?**
No. La promedio es la generación **estimada** de la planta; la contratada es
`cantidad_minima_kwh_mes` del PPA, un compromiso comercial. Son cosas distintas y pueden no
coincidir.

**¿`contrato_compra_meses` cuenta días o meses?**
Meses calendario, contando el primero y el último. Es el mismo conteo con que se generan las
tarifas mensuales del PPA, así que duración y facturación no pueden divergir. Un contrato
del 12-feb-2026 al 31-dic-2032 son 83 meses.

**¿La ficha viene en todos los endpoints que devuelven una oferta?**
Sí — lista plana, detalle del cliente, ofertas del cliente, crear oferta y registrar
seguimiento. Hay un test que lo verifica en los cinco.

**¿A quién le escribimos?**
A Juan José (juanjose@unergy.io) para keys, roles, ids de operadores de red y para saber si
ya está desplegado.
