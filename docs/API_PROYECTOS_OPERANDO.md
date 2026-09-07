# API de Proyectos Firmados y en Operación

> ⚠️ **Documento histórico.** Describe la forma **anterior** de este endpoint: una fila
> por planta en `items[]`. Desde el 2026-08-18 la respuesta es un árbol **PPA → PROYECTOS
> → detalles** en `ppas[]` y `items` ya no existe. La ficha de cada planta creció el
> 2026-08-19 (identificación, clasificación, técnica, fronteras, servicios, obra,
> simulación). **La documentación vigente es [`API_PPA_PIPELINE.md`](API_PPA_PIPELINE.md).**
> Esto se conserva para entender de dónde viene la forma vieja y para quien todavía tenga
> un integrador escrito contra ella.

Una sola llamada devuelve **todas las plantas con negocio cerrado** — firmadas y operando —
con estos datos por planta (y con `?todas_las_etapas=true`, todo el pipeline comercial):

**nombre · estado comercial · estado del proyecto · ubicación · operador de red · generación mensual promedio · fecha de
inicio de comercialización · tiempo del contrato de energía · API ID de Unergy.**

Es la misma lista que se ve en la plataforma en `/comercial` filtrando por las etapas
**Firmado** y **Operando**, pero agrupada por planta y en un JSON pensado para integrar.

- **Base URL:** `https://operaciones.unergy.io`
- **Endpoint:** `GET /api/v1/comercial/proyectos-operando`
- **Sin Swagger interactivo.** La documentacion automatica la servia FastAPI en `/docs`, y desapareció con la migración a Django (2026-09-04). Esta guía es la referencia.
- **Solo lectura.** Este endpoint no escribe nada; no hay forma de romper datos con él.

---

## 1. Autenticación

Header `X-API-Key` en cada request. La key la emite Juan José desde la plataforma.

```bash
export UNERGY_API_KEY="uop_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export BASE="https://operaciones.unergy.io/api/v1"

# ¿la key sirve?
curl "$BASE/api-keys/verify" -H "X-API-Key: $UNERGY_API_KEY"
# → {"user_id": 12, "nombre": "...", "rol": "operaciones"}
```

**Sirve cualquier rol** (`operaciones`, `monitoreo`, `comercial`, `admin`): a diferencia del
resto de `/comercial`, este endpoint no exige rol comercial porque no expone precios,
márgenes ni bitácora comercial. Si `verify` responde 200, el endpoint responde 200.

La key hereda todos los permisos del usuario dueño: trátenla como una contraseña, guárdenla
en variable de entorno y **nunca** la pongan en un frontend ni en el repo.

---

## 2. Quickstart

```bash
curl "$BASE/comercial/proyectos-operando" -H "X-API-Key: $UNERGY_API_KEY"
```

Respuesta:

```json
{
  "generado_en": "2026-08-10T09:21:49-05:00",
  "estados_pipeline": ["firmado", "operando"],
  "total": 38,
  "por_estado_pipeline": { "firmado": 7, "operando": 31 },
  "items": [ { … una entrada por planta … } ]
}
```

`items` no está paginado: vienen todas. Hoy son unas decenas y la llamada tarda menos de un
segundo.

---

## 3. Una planta, entera

```json
{
  "proyecto_id": 9,
  "nombre": "La Catedral",
  "estado_pipeline": "operando",
  "estado_proyecto": "en_operacion",
  "estado_proyecto_label": "En operación",
  "api_id_unergy": "catedral",
  "ubicacion": {
    "municipio": "Corozal",
    "departamento": "Sucre",
    "texto": "Corozal, Sucre",
    "latitud": 9.317,
    "longitud": -75.293
  },
  "operador_red": "AFINIA S.A.S. E.S.P.",
  "operador_red_id": 7,
  "gen_promedio_mensual_mwh": 178.412,
  "gen_promedio_mensual_kwh": 178412.0,
  "gen_promedio_origen": "medido",
  "gen_promedio_detalle": {
    "dias_con_datos": 30,
    "ventana_desde": "2026-07-10",
    "ventana_hasta": "2026-08-08",
    "actualizado_en": "2026-08-09T21:18:45-05:00"
  },
  "fecha_inicio_comercializacion": "2026-02-12",
  "fecha_entrada_operacion": "2026-02-01",
  "contrato_energia": {
    "fecha_inicio": "2026-02-12",
    "fecha_fin": "2032-12-31",
    "duracion_meses": 83,
    "duracion_anios": 6.9,
    "duracion_texto": "6 años y 11 meses",
    "meses_restantes": 77,
    "vigente": true,
    "ppa_contrato_id": 1,
    "numero_codigo_contrato": "UNG-2026-014",
    "nombre_interno": "PPA Catedral",
    "tipo": "compra",
    "comprador": "UNERGY S.A.S.",
    "vendedor": "INVERSIONES TECNI-PLAST S.A.S.",
    "cantidad_minima_kwh_mes": 150000.0
  },
  "cliente": "INVERSIONES TECNI-PLAST S.A.S.",
  "potencia_instalada_kwp": 990.0,
  "oferta_vigente": {
    "oferta_id": 1,
    "codigo_seguimiento": "OP.COM No.0051-3-2026",
    "tipo": "compra_energia",
    "estado": "operando",
    "oportunidad_id": 2
  },
  "ofertas": [
    {
      "oferta_id": 1,
      "codigo_seguimiento": "OP.COM No.0051-3-2026",
      "tipo": "compra_energia",
      "estado": "operando",
      "oportunidad_id": 2
    }
  ],
  "fuentes": {
    "nombre": "proyecto",
    "municipio": "proyecto",
    "departamento": "proyecto",
    "operador_red": "proyecto",
    "gen_promedio_mensual": "medido",
    "estado_proyecto": "proyecto",
    "fecha_inicio_comercializacion": "proyecto",
    "contrato_energia": "contrato",
    "api_id_unergy": "sub_project"
  }
}
```

### Los campos pedidos

| Lo que pidieron | Dónde está |
|---|---|
| Nombre | `nombre` |
| Estado (firmado / operando) | `estado_pipeline` |
| Estado del proyecto (En desarrollo / En operación / Suspendido / Cancelado) | `estado_proyecto` + `estado_proyecto_label` |
| Ubicación | `ubicacion.texto` (o `municipio` / `departamento` por separado) |
| Operador de red | `operador_red` |
| Generación mensual promedio | `gen_promedio_mensual_mwh` (y `…_kwh`) |
| Fecha de inicio de comercialización | `fecha_inicio_comercializacion` |
| Tiempo del contrato de energía | `contrato_energia.duracion_texto` (y `duracion_meses` / `duracion_anios`) |
| API ID de Unergy | `api_id_unergy` |

### Referencia completa

| Campo | Tipo | Qué es |
|---|---|---|
| `proyecto_id` | `int \| null` | Id de la planta en la plataforma. **`null`** = la planta todavía no existe como proyecto, los datos vienen de la oferta comercial |
| `nombre` | `string \| null` | Nombre de la planta |
| `estado_pipeline` | `string` | Etapa comercial de la planta. Por defecto `firmado` u `operando`; con `?todas_las_etapas=true` también `oportunidad`, `oferta`, `contrato`, `terminado` y `declinado` — ver la sección 4 |
| `estado_proyecto` | `string \| null` | Estado de la planta: `en_desarrollo` · `en_operacion` · `suspendido` · `cancelado`. `null` si `proyecto_id` es `null` — ver la sección 4 |
| `estado_proyecto_label` | `string \| null` | El mismo estado listo para mostrar: `"En operación"`. Úsenlo en vez de armar el mapa de su lado |
| `api_id_unergy` | `string \| null` | Con qué id se consulta esta planta en la API de Unergy (`sub_project`). `null` = todavía no tiene identificador de monitoreo cargado |
| `ubicacion.municipio` | `string \| null` | Municipio |
| `ubicacion.departamento` | `string \| null` | Departamento |
| `ubicacion.texto` | `string \| null` | `"Municipio, Departamento"` armado; `null` si no hay ninguno de los dos |
| `ubicacion.latitud` / `longitud` | `float \| null` | Coordenadas, si la planta las tiene cargadas |
| `operador_red` | `string \| null` | **Nombre legal** del operador de red |
| `operador_red_id` | `int \| null` | Su id en el catálogo `operadores_red` |
| `gen_promedio_mensual_mwh` | `float \| null` | Generación de un mes típico, en **MWh** |
| `gen_promedio_mensual_kwh` | `float \| null` | Lo mismo en **kWh** (× 1000, ya convertido) |
| `gen_promedio_origen` | `string \| null` | `medido` · `manual` · `estimado` · `declarado` — ver abajo |
| `gen_promedio_detalle.dias_con_datos` | `int \| null` | Cuántos días con lectura entraron al promedio (de 30) |
| `gen_promedio_detalle.ventana_desde` / `hasta` | `date \| null` | Qué ventana se promedió |
| `gen_promedio_detalle.actualizado_en` | `datetime \| null` | Cuándo se calculó |
| `fecha_inicio_comercializacion` | `date \| null` | Primer día con generación real de energía |
| `fecha_entrada_operacion` | `date \| null` | Entrada en operación de la planta — **otro hecho**, no confundir |
| `contrato_energia.fecha_inicio` / `fecha_fin` | `date \| null` | Periodo del contrato |
| `contrato_energia.duracion_meses` | `int \| null` | Duración en meses calendario |
| `contrato_energia.duracion_anios` | `float \| null` | La misma duración en años, 1 decimal |
| `contrato_energia.duracion_texto` | `string \| null` | `"6 años y 11 meses"` — listo para mostrar |
| `contrato_energia.meses_restantes` | `int \| null` | Meses que le quedan desde hoy; `0` si ya venció. Si **todavía no arrancó** (etapa `firmado`) es su duración completa |
| `contrato_energia.vigente` | `bool \| null` | Si el contrato está corriendo hoy |
| `contrato_energia.ppa_contrato_id` | `int \| null` | Id del contrato en la plataforma |
| `contrato_energia.numero_codigo_contrato` | `string \| null` | Código del contrato |
| `contrato_energia.tipo` | `string \| null` | `compra` (Unergy le compra la energía a la planta) o `venta` |
| `contrato_energia.comprador` / `vendedor` | `string \| null` | Las partes |
| `contrato_energia.cantidad_minima_kwh_mes` | `float \| null` | Energía comprometida por mes. **No es lo mismo que la generación promedio** |
| `cliente` | `string \| null` | Razón social del cliente dueño del negocio |
| `potencia_instalada_kwp` | `float \| null` | Potencia instalada |
| `oferta_vigente` | `object \| null` | **La** oferta que sostiene la etapa de la planta. Misma forma que un elemento de `ofertas[]`. `null` si la planta solo tiene ofertas `terminado`/`declinado` — ver la sección 4 |
| `ofertas[]` | `array` | **Todas** las ofertas comerciales de esa planta, de todo el pipeline, cada una con su propia etapa en `estado` |
| `fuentes` | `object` | De dónde salió cada dato — ver abajo |

**Todas las fechas son `YYYY-MM-DD`.** Los `datetime` van en ISO 8601 con offset de Colombia
(`-05:00`).

---

## 4. Los dos estados: `estado_pipeline` y `estado_proyecto`

Cada fila trae **dos estados que responden preguntas distintas**. No son sinónimos y no hay
que elegir uno: conviene mostrar los dos.

| Campo | Responde | Valores |
|---|---|---|
| `estado_pipeline` | ¿En qué punto está el **negocio**? | `oportunidad` · `oferta` · `contrato` · `firmado` · `operando` · `terminado` · `declinado` |
| `estado_proyecto` | ¿En qué punto está la **planta** en la plataforma? | `en_desarrollo` · `en_operacion` · `suspendido` · `cancelado` · `null` |

### `estado_pipeline` — la etapa comercial

**Por defecto solo vienen las dos de negocio cerrado**, que son las que casi siempre importan:

| Valor | Qué significa | Qué esperar de los datos |
|---|---|---|
| `firmado` | Hay contrato, el **suministro todavía no arrancó** | Es normal que `gen_promedio_mensual_mwh` y `fecha_inicio_comercializacion` vengan en `null`: la planta aún no entrega energía. `contrato_energia.vigente` es `false` y `fecha_inicio` está en el futuro |
| `operando` | Ya está entregando energía | Debería tener generación promedio y fecha de inicio de comercialización |

Con `?todas_las_etapas=true` viene el pipeline completo. Las otras cinco:

| Valor | Qué significa |
|---|---|
| `oportunidad` | Prospecto: hay interés, todavía no hay oferta |
| `oferta` | Se mandó la oferta, esperando respuesta |
| `contrato` | En negociación del contrato |
| `terminado` | **Salida.** El negocio corrió y el contrato se venció |
| `declinado` | **Salida.** No se cerró |

En esas cinco es normal que casi todo venga en `null`: la planta puede ni existir todavía como
proyecto en la plataforma (`proyecto_id: null`), y sin proyecto no hay generación, ni operador,
ni contrato.

#### Cuando una planta tiene varias ofertas

`estado_pipeline` es la etapa **más avanzada** de todas: una con la energía operando y los
servicios recién firmados sale como `operando`. Con una regla encima: **`terminado` y
`declinado` son salidas, no avances**, así que cualquier etapa viva les gana. Una planta que
está entregando energía y además arrastra un contrato viejo terminado sigue siendo `operando`,
no `terminado`.

Por eso **cada planta cae en una sola etapa** y sumar los conteos de `por_estado_pipeline` da
exactamente el `total`, sin repetidas. La etapa de cada oferta por separado está en `ofertas[]`.

### `estado_proyecto` — el estado de la planta

El mismo estado que se ve en el módulo de proyectos de la plataforma. Viene con su etiqueta ya
armada en `estado_proyecto_label` (`"En operación"`, `"En desarrollo"`, `"Suspendido"`,
`"Cancelado"`): úsenla en vez de traducir el slug de su lado, así no se desalinea el día que
agreguemos un estado nuevo.

Es **`null`** cuando `proyecto_id` es `null`: la oferta todavía no está vinculada a una planta
cargada en la plataforma, así que no hay estado que dar. `fuentes.estado_proyecto` lo confirma.

> **Los dos estados pueden discrepar y no se concilian.** Hay plantas con la oferta `operando`
> y el proyecto todavía en `en_desarrollo` (o marcado `cancelado`): son dos hechos que se
> cargan por caminos distintos, y la API los muestra como están en vez de inventar coherencia.
> Si les aparece una discrepancia que les afecta, avísennos: es dato por corregir de nuestro
> lado, no un error de la respuesta.

Para el semáforo de una planta, `estado_pipeline` es el que manda: dice si el negocio está
entregando energía. `estado_proyecto` agrega el matiz operativo (`suspendido` es la señal de
que algo pasa con la planta aunque el contrato siga vivo).

Para pedir solo las que ya operan:

```bash
curl "$BASE/comercial/proyectos-operando?estado_pipeline=operando" -H "X-API-Key: $UNERGY_API_KEY"
```

`por_estado_pipeline` en el sobre trae el conteo de cada etapa, así no hay que contarlas.

> **Ojo con `contrato_energia.meses_restantes` en las firmadas:** si el contrato todavía no
> arrancó, ese número es su duración completa (no la distancia hasta la fecha de fin). Nunca
> es mayor que `duracion_meses`.

### Por qué una planta firmada puede no tener `contrato_energia`

`contrato_energia` es el **contrato de compra de energía** (el PPA). Una planta cuyo negocio
cerrado es de **servicios operacionales** (representación, CGM) no tiene PPA: su contrato es
de otro tipo y no vive en ese campo. En esos casos `contrato_energia` viene en `null` y no es
un dato faltante — es que no aplica.

Para saber en cuál de los dos casos están, miren `ofertas[].tipo`:

| `ofertas[].tipo` | Qué esperar de `contrato_energia` |
|---|---|
| `compra_energia` | Debería tener el PPA. Si viene `null`, ahí sí falta cargarlo |
| `servicios_operacionales` | Normal que venga `null`: no hay contrato de energía |
| `comunidad_energetica` | Depende del negocio |

Hoy las 5 plantas en etapa `firmado` son todas de servicios (`OP.REPCGM…`), así que ninguna
trae `contrato_energia`.

---

## 5. `gen_promedio_origen` — léanlo siempre

El número está siempre en la misma casilla, pero no siempre vale lo mismo. **Muestren el
origen al lado del número** (aunque sea un ícono o un tooltip):

| Valor | Qué significa | Confiabilidad |
|---|---|---|
| `medido` | Promedio de la generación **real** de los últimos 30 días | Alta — es el dato duro |
| `manual` | Lo cargó una persona porque la planta no tiene histórico (recién energizada) | Media |
| `estimado` | Proyección de ingeniería de la planta (curva P50), no medición | Baja para operación |
| `declarado` | Lo declaró la oferta comercial; la planta aún no existe en la plataforma | Baja |
| `null` | Nadie lo aportó todavía. **No es un error** | — |

Con `medido`, `gen_promedio_detalle.dias_con_datos` dice sobre cuántos días se promedió: 30
es una ventana completa, 27 es una ventana con huecos de monitoreo. No vale lo mismo.

---

## 6. `fuentes` — distinguir "no aplica" de "todavía no lo sabemos"

Sin este mapa, un dato que no aplica y un dato que falta se ven idénticos (los dos `null`).

| Valor | Significa |
|---|---|
| `"proyecto"` | Salió de la planta cargada en la plataforma |
| `"proyecto_legacy"` | Salió de un campo de texto viejo, sin validar contra el catálogo. Úsenlo, pero sepan que puede estar mal escrito |
| `"oferta"` | Lo declaró la oferta comercial (la planta aún no existe como proyecto) |
| `"contrato"` | Salió del contrato PPA |
| `"medido"` / `"manual"` / `"estimado"` / `"declarado"` | Solo para `gen_promedio_mensual`, ver arriba |
| `"sub_project"` | Solo para `api_id_unergy`: de qué campo salió el id |
| `null` | **Nadie lo aportó todavía.** No es un error |

`fuentes` siempre trae estas 9 llaves: `nombre`, `municipio`, `departamento`, `operador_red`,
`gen_promedio_mensual`, `estado_proyecto`, `fecha_inicio_comercializacion`, `contrato_energia`,
`api_id_unergy`.

---

## 7. Tres cosas que conviene saber antes de integrar

**1. La forma de la respuesta nunca cambia.** Todas las llaves están siempre, aunque el valor
sea `null`. No hace falta programar defensivamente contra llaves ausentes — sí contra valores
`null`.

**2. `fecha_inicio_comercializacion` no se rellena con nada.** Son tres hechos distintos y
cada uno tiene su casilla:

- `fecha_inicio_comercializacion` → primer día con generación real de energía
- `fecha_entrada_operacion` → cuándo entró en operación la planta
- `contrato_energia.fecha_inicio` → cuándo arranca el suministro del contrato

Si necesitan "desde cuándo produce", es la primera. Si viene `null`, es que ese dato todavía
no está cargado en la plataforma — háblenlo con Juan José, no lo sustituyan por otra fecha.

**3. Una planta = una fila, aunque tenga varias ofertas.** Una planta suele tener la oferta de
compra de energía y la de servicios operacionales. Salen agrupadas en una sola entrada, con
los dos códigos en `ofertas[]`. No hay que deduplicar.

Para el caso normal no hace falta recorrer esa lista: **`oferta_vigente`** trae suelta la que
sostiene la etapa de la planta (si dos están en la misma etapa, manda la de compra de energía,
que es la que define el negocio). Se cumple siempre que `oferta_vigente.estado ==
estado_pipeline`. Es `null` —y solo entonces— cuando la planta no tiene nada vivo: todas sus
ofertas están `terminado` o `declinado`. `ofertas[]` las sigue trayendo todas.

---

## 8. Filtros

Los dos son opcionales y se combinan:

| Parámetro | Qué hace |
|---|---|
| `estado_pipeline` | Cualquier etapa del pipeline: `oportunidad` · `oferta` · `contrato` · `firmado` · `operando` · `terminado` · `declinado`. Repetible. Por defecto vienen `firmado` y `operando`. **Filtra por la etapa comercial, no por `estado_proyecto`** |
| `todas_las_etapas` | `true` trae el pipeline completo, las 7 etapas. Se ignora si se pasan etapas explícitas (gana lo más específico) |
| `q` | Busca texto en el nombre de la planta, la razón social del cliente y el código de seguimiento |

```bash
# solo las que ya operan
curl "$BASE/comercial/proyectos-operando?estado_pipeline=operando" -H "X-API-Key: $UNERGY_API_KEY"

# solo las firmadas que aún no arrancan
curl "$BASE/comercial/proyectos-operando?estado_pipeline=firmado" -H "X-API-Key: $UNERGY_API_KEY"

# todo el pipeline: prospeccion, negociacion, cerradas, terminadas y declinadas
curl "$BASE/comercial/proyectos-operando?todas_las_etapas=true" -H "X-API-Key: $UNERGY_API_KEY"

# etapas sueltas, repitiendo el parametro
curl "$BASE/comercial/proyectos-operando?estado_pipeline=contrato&estado_pipeline=firmado" \n  -H "X-API-Key: $UNERGY_API_KEY"

# por nombre
curl "$BASE/comercial/proyectos-operando?q=catedral" -H "X-API-Key: $UNERGY_API_KEY"
```

Algo que no sea una etapa del pipeline da **422**, no una lista vacía: recibir 200 con cero filas
se leería como "no hay ninguna", que es otra cosa que "esa etapa no existe".

`GET /comercial/ofertas?estado=…` sigue siendo la vista por OFERTA en vez de por planta (ese sí
pide rol `comercial` o `admin`).

---

## 9. Ejemplos

### Python

```python
import os
import requests

BASE = "https://operaciones.unergy.io/api/v1"
KEY = os.environ["UNERGY_API_KEY"]

r = requests.get(f"{BASE}/comercial/proyectos-operando",
                 headers={"X-API-Key": KEY}, timeout=60)
r.raise_for_status()
datos = r.json()

print(f"{datos['total']} plantas al {datos['generado_en']} — {datos['por_estado_pipeline']}")
for p in datos["items"]:
    gen = p["gen_promedio_mensual_mwh"]
    # el origen importa tanto como el número: medido != estimado
    gen_txt = f"{gen:,.1f} MWh/mes ({p['gen_promedio_origen']})" if gen is not None else "sin dato"
    print(f"{p['estado_pipeline']:9} {p['nombre']:35} {p['api_id_unergy'] or '—':18} "
          f"{p['ubicacion']['texto'] or '—':30} {p['operador_red'] or '—':25} "
          f"{gen_txt:28} {p['contrato_energia']['duracion_texto'] or '—'}")

# solo las que ya entregan energía
operando = [p for p in datos["items"] if p["estado_pipeline"] == "operando"]

# el código de seguimiento del negocio, sin recorrer la lista de ofertas
for p in datos["items"]:
    vigente = p["oferta_vigente"]        # None si no tiene nada vivo
    print(p["nombre"], vigente["codigo_seguimiento"] if vigente else "sin oferta vigente")

# todo el pipeline
todo = requests.get(f"{BASE}/comercial/proyectos-operando",
                    params={"todas_las_etapas": "true"},
                    headers={"X-API-Key": KEY}, timeout=60).json()
```

### JavaScript

```js
const BASE = "https://operaciones.unergy.io/api/v1";

const res = await fetch(`${BASE}/comercial/proyectos-operando`, {
  headers: { "X-API-Key": process.env.UNERGY_API_KEY },
});
if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
const { total, por_estado_pipeline, items } = await res.json();

console.log(total, por_estado_pipeline);   // 38 { firmado: 7, operando: 31 }

items.forEach((p) => {
  console.log(
    p.estado_pipeline,            // "firmado" | "operando" | …
    p.estado_proyecto_label,      // "En operación" | null
    p.oferta_vigente?.codigo_seguimiento ?? "—",
    p.nombre,
    p.api_id_unergy ?? "—",
    p.ubicacion.texto ?? "—",
    p.operador_red ?? "—",
    p.gen_promedio_mensual_mwh ?? "—",
    p.fecha_inicio_comercializacion ?? "—",
    p.contrato_energia.duracion_texto ?? "—",
  );
});
```

### Pasar a Excel / CSV

```bash
curl -s "$BASE/comercial/proyectos-operando" -H "X-API-Key: $UNERGY_API_KEY" \
| jq -r '["estado_pipeline","estado_proyecto","nombre","api_id_unergy","ubicacion","operador","gen_mwh_mes","origen","inicio_comercializacion","contrato"],
         (.items[] | [.estado_pipeline, .estado_proyecto, .nombre, .api_id_unergy, .ubicacion.texto, .operador_red,
                      .gen_promedio_mensual_mwh, .gen_promedio_origen,
                      .fecha_inicio_comercializacion,
                      .contrato_energia.duracion_texto]) | @csv' > plantas.csv
```

---

## 10. Errores

| Código | Cuándo | Qué hacer |
|---|---|---|
| 401 | `Token requerido` — no mandaron el header `X-API-Key` | Revisar el header |
| 401 | `API Key inválida` — la key no existe o está desactivada | Pedirle una nueva a Juan José |
| 401 | `Usuario inactivo o no encontrado` | El usuario dueño de la key se desactivó |
| 422 | `Etapa no válida: …` | `estado_pipeline` recibió algo que no es una etapa del pipeline. El mensaje lista las que sí valen |
| 422 | Parámetro mal formado | Revisar `q` |

Un resultado vacío **no es un error**: responde 200 con `total: 0` e `items: []`.

---

## 11. Recomendaciones de uso

- **Cachéen del lado de ustedes.** Los datos cambian a lo sumo una vez al día (la generación
  promedio se recalcula por lote). Llamar cada 5 minutos no aporta nada; una vez por hora
  sobra.
- **Timeout de 60 s** en el cliente. La llamada tarda menos de un segundo, pero Railway puede
  tener un arranque en frío.
- **Reintenten con espera** ante un 5xx: un reintento a los 30 s, no un bucle.
- **`generado_en`** dice de cuándo es la foto. Muéstrenla si el tablero se cachea.

---

## 12. Contacto

Juan José (juanjose@unergy.io) para API keys, para reportar datos que salgan en `null` y para
pedir campos nuevos.
