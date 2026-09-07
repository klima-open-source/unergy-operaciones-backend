# API de Cumplimiento y Generación — Plataforma Operaciones Unergy

Guía para consultar **compromisos mínimo/máximo de energía por contrato** y **generación de los proyectos** de forma programática. Es exactamente la data que alimenta la vista `/mem/cumplimiento`.

- **Base URL:** `https://operaciones.unergy.io/api/v1`
  (también funciona directo contra el backend: `https://operaciones.unergy.io/api/v1`)
- **Sin Swagger interactivo.** La documentacion automatica la servia FastAPI en `/docs`, y desapareció con la migración a Django (2026-09-04). Esta guía es la referencia.
- **Formato:** JSON. Todo lo de esta guía es `GET` — **no se necesita escribir nada**.
- **Unidades:** energía en **MWh** salvo donde diga `kwh` explícitamente. Precios en **COP/kWh**.

---

## ⚠️ Lo primero: la generación **ya viene incluida**

No hay que darle acceso a la API de monitoreo de Unergy ni a sus credenciales.

La plataforma llama a esa API **del lado del servidor** con las credenciales de Unergy, cruza los kWh con las asignaciones de GESCON (`% de despacho` de cada planta a cada contrato) y devuelve el resultado ya consolidado. Desde afuera se ve como un solo endpoint.

```
  ella ──X-API-Key──▶ Plataforma Operaciones ──▶ API Unergy (project_generation)
                             │                        credenciales del servidor
                             ├──▶ BD: compromisos PPA (mín / máx por mes)
                             ├──▶ BD: GESCON (qué planta despacha a qué contrato y en qué %)
                             └──▶ BD: precios de bolsa
                             ▼
                      compromiso vs generación, ya calculado
```

Con la API Key de la plataforma le alcanza para todo. Está probado: todos los endpoints de abajo responden `200` con esa key.

---

## 0. El universo de contratos: empresa responsable

Cada PPA tiene una **empresa responsable** (normalmente Unergy; algunos los gestiona un tercero). Los endpoints la devuelven aplanada en cada fila de contrato:

```json
{ "id": 12, "nombre_interno": "BIA Naos 1", "comprador_nombre": "…",
  "responsable_id": 2, "responsable": "Externo", "responsable_relevante": false }
```

**Todos los endpoints de esta guía omiten por defecto los contratos cuyo responsable tiene `incluir_en_cumplimiento = false`**, igual que la vista `/mem/cumplimiento`. Pase `incluir_todos=true` a cualquiera de ellos para traerlos:

| Endpoint | Filtra por defecto |
|---|---|
| `/cumplimiento/ppa`, `/ppa/resumen`, `/ppa/resumen-anual` | sí |
| `/cumplimiento/simulador`, `/plantas-contratos`, `/energia-transada` | sí |
| `/cumplimiento/balance-energia` | sí (lo hereda de `plantas-contratos`) |
| `/cumplimiento/anual-matriz`, `/anual-matriz/contratos` | sí |
| `/cumplimiento/panel-anual` | sí (`incluir_todos` va en la llave de caché) |
| `/cumplimiento/ppa/{id}/anual`, `/anual-matriz/contrato/{id}`, `/ppa/{id}` | **no** — son detalle por id, responden para cualquier contrato |
| `/cumplimiento/descubrimientos`, `POST /cumplimiento/cerrar-periodo` | **no** — a propósito (ver abajo) |

Reglas:

- Un contrato **sin responsable** (`responsable_id: null`) **siempre se incluye**. Nada se esconde por omisión, solo por marca explícita.
- `cerrar-periodo` no filtra porque **persiste** el cierre mensual: dejar contratos fuera cambiaría el histórico guardado, y marcar un responsable como no relevante borraría su cierre.
- `descubrimientos` no filtra porque existe para destapar exposición en bolsa, no para esconderla.
- Si dos endpoints deben cuadrar entre sí, páseles el **mismo** `incluir_todos`; si no, uno suma un universo y el otro, otro.

El catálogo vive en `/ppa/responsables`: `GET` lista con `n_contratos`; `POST` crea; `PATCH /{id}` renombra o cambia `incluir_en_cumplimiento`; `DELETE /{id}` da `409` si aún tiene contratos; `POST /ppa/responsables/asignar` con `{contrato_ids, responsable_id}` asigna en bloque (`responsable_id: null` desasigna). En el contrato, `responsable_id` es un campo más de `POST /ppa` y `PATCH /ppa/{id}`.

---

## 1. Autenticación

Header `X-API-Key` en cada request:

```bash
curl -H "X-API-Key: uop_xxxx..." \
  "https://operaciones.unergy.io/api/v1/cumplimiento/ppa/resumen?year=2026&month=7"
```

La key hereda el rol y permisos del usuario al que se le creó. Los endpoints de esta guía solo exigen estar autenticado (no filtran por rol), así que ve la totalidad de los contratos.

---

## 2. El endpoint principal: todo en una sola llamada

### `GET /cumplimiento/ppa/resumen?year={YYYY}&month={M}`

**Este resuelve el 90% de lo que necesita.** Devuelve, para *todos* los contratos vigentes en ese mes, el compromiso mínimo, el máximo y la generación real cruzada contra ellos.

```bash
curl -H "X-API-Key: $KEY" "$BASE/cumplimiento/ppa/resumen?year=2026&month=7"
```

Respuesta (recortada, datos reales de julio 2026):

```json
{
  "periodo": {
    "year": 2026, "month": 7,
    "dia_actual": 31, "dias_mes": 31,
    "es_mes_actual": false, "es_mes_futuro": false,
    "tipo_datos": "real",
    "dia_min_datos": 30, "dia_max_datos": 31
  },
  "totales": {
    "energia_minima_mwh": 187491.233,
    "energia_maxima_mwh": 190507.81,
    "gen_total_mwh": 8695.637,
    "gen_proyectada_mwh": 8695.637,
    "estado": "deficit",
    "compras_bolsa_mwh": 178795.596,
    "excedentes_bolsa_mwh": 0.0
  },
  "valoracion_bolsa": { "precio_bolsa_avg_cop_kwh": 0, "compras_bolsa_cop": 0, "…": "…" },
  "contratos": [
    {
      "id": 18,
      "nombre_interno": "BIA Delta 1",
      "numero_codigo_contrato": "EV-ENER-003-2025",
      "comprador_nombre": "Bia Energy S.A.S.",

      "energia_minima_mwh": 126.0,          // ← COMPROMISO MÍNIMO
      "energia_maxima_mwh": null,           // ← COMPROMISO MÁXIMO (null = sin techo)

      "gen_total_mwh": 172.057,             // ← GENERACIÓN despachada al contrato
      "gen_proyectada_mwh": 172.057,        // proyección a fin de mes (mes en curso)

      "estado": "ok",                       // ok | deficit | excedente | sin_compromisos
      "compras_bolsa_mwh": 0.0,             // faltante que toca comprar en bolsa
      "excedentes_bolsa_mwh": 0.0,          // sobrante que se vende en bolsa
      "compras_bolsa_cop": null,
      "excedentes_bolsa_cop": null,

      "n_plantas_activas": 1,
      "plantas_registradas": 1,
      "plantas_esperadas": 0,
      "plantas_sin_datos": [],
      "dia_min_datos": 31,

      "exposicion_bolsa_duplicados_mwh": null,
      "uso_recurso_mwh": null,
      "energia_perdida_mantenimiento_mwh": null,
      "gen_disponible_mwh": 172.057,
      "compras_bolsa_ajustada_mwh": 0.0,
      "riesgo_penalizacion_mantenimiento": false
    }
  ]
}
```

**Campos que importan para su caso:**

| Campo | Significado |
|---|---|
| `energia_minima_mwh` | Compromiso **mínimo** del contrato ese mes. `null` = no hay compromiso cargado |
| `energia_maxima_mwh` | Compromiso **máximo**. `null` = contrato sin techo |
| `gen_total_mwh` | Energía realmente despachada al contrato = Σ (generación de cada planta × su % de despacho) |
| `gen_proyectada_mwh` | Proyección lineal al cierre del mes. En meses cerrados es igual a `gen_total_mwh` |
| `estado` | Comparación ya hecha: `deficit` (< mín), `ok`, `excedente` (> máx), `sin_compromisos` |
| `compras_bolsa_mwh` | `max(0, mínimo − generación)` — lo que hay que comprar |
| `excedentes_bolsa_mwh` | `max(0, generación − máximo)` — lo que sobra |

**Ojo con `tipo_datos`** (en `periodo`), porque cambia cómo se lee `gen_*`:

- `real` — mes cerrado, son kWh medidos.
- `proyeccion_lineal` — mes en curso: `gen_total_mwh` es lo acumulado hasta hoy y `gen_proyectada_mwh` lo extrapola a fin de mes.
- `proyeccion_historica` — mes futuro: se estima con el promedio diario de los últimos ~15 días con datos.

`dia_min_datos` / `dia_max_datos` dicen hasta qué día del mes hay lecturas — sirve para detectar plantas rezagadas.

---

## 3. Detalle de un contrato

### `GET /cumplimiento/ppa/{contrato_id}?year={YYYY}&month={M}`

Lo mismo que arriba pero para un solo contrato, **con el desglose planta por planta**, la tarifa PPA y la valoración contra el precio de bolsa.

```json
{
  "contrato": { "id": 18, "nombre_interno": "BIA Delta 1", "fecha_inicio": "…", "fecha_fin": "…" },
  "periodo":  { "…": "…" },
  "compromisos": { "energia_minima_mwh": 126.0, "energia_maxima_mwh": null },
  "generacion": {
    "gen_total_mwh": 172.057,
    "gen_proyectada_mwh": 172.057,
    "tarifa_cop_kwh": 0,
    "plantas": [
      {
        "nombre": "GD Delta 1",
        "sub_project": "delta_1",
        "pct_despacho": 1.0,          // fracción 0–1, NO porcentaje 0–100
        "gen_planta_mwh": 172.057,    // lo que generó la planta
        "gen_contrato_mwh": 172.057,  // gen_planta × pct_despacho
        "es_duplicado": false,
        "sin_datos": false,
        "sin_api_id": false
      }
    ],
    "plantas_sin_datos": []
  },
  "balance": { "estado": "ok", "compras_bolsa_mwh": 0.0, "excedentes_bolsa_mwh": 0.0, "margen_mwh": null },
  "valoracion_bolsa": { "precio_bolsa_avg_cop_kwh": 0, "tarifa_ppa_cop_kwh": 0, "…": "…" }
}
```

### `GET /cumplimiento/ppa`

Catálogo de contratos para el selector: `id`, `nombre_interno`, `numero_codigo_contrato`, `comprador_nombre`, `fecha_inicio`, `fecha_fin`. Es la forma barata de resolver `contrato_id`.

---

## 4. Series anuales

### `GET /cumplimiento/ppa/{contrato_id}/anual?year={YYYY}`

Los 12 meses de un contrato: compromisos vs generación mes a mes.

```json
{
  "contrato": { "id": 18, "nombre_interno": "BIA Delta 1" },
  "year": 2026,
  "meses": [
    {
      "month": 7,
      "min_mwh": 126.0,
      "max_mwh": null,
      "gen_mwh": 172.057,
      "gen_proyectada_cierre": null,
      "estado": "ok",
      "tipo_datos": "real",
      "compras_bolsa_mwh": 0.0,
      "excedentes_bolsa_mwh": 0.0,
      "plantas": [ { "nombre": "GD Delta 1", "sub_project": "delta_1",
                     "pct_despacho": 1.0, "dias_en_contrato": 31, "dias_mes": 31,
                     "gen_planta_mwh": 172.057, "gen_contrato_mwh": 172.057 } ],
      "n_plantas": 1
    }
  ]
}
```

### `GET /cumplimiento/ppa/resumen-anual?year={YYYY}`

**Rápido: solo lee base de datos, no llama a la API de generación.** Totales de compromiso del año por contrato (`total_min_mwh`, `total_max_mwh`, `meses_con_compromisos`). Ideal si solo necesita los compromisos, sin generación.

### `GET /cumplimiento/anual-matriz?year={YYYY}`

Matriz completa contrato × 12 meses (lo que exporta el Excel de la pestaña *Matriz anual*). Es el endpoint **más pesado** de todos — tráigalo una vez y cachéelo.

Auxiliares: `GET /cumplimiento/anual-matriz/contratos?year=` (lista liviana) y `GET /cumplimiento/anual-matriz/contrato/{id}?year=` (una fila).

Ver la sección 0 sobre el universo de contratos: por defecto omite los de responsable no relevante.

---

## 5. Generación cruda (si necesita la curva, no el cumplimiento)

Los endpoints anteriores ya traen generación **agregada por mes y cruzada con el contrato**. Estos son para cuando quiera la serie de tiempo de una planta.

### `GET /monitoreo/_legacy?action=getProjects`

Catálogo de plantas en operación. Devuelve el `sub_project`, que es **la llave** para pedir generación.

```json
{ "ok": true, "projects": [
  { "sub_project": "acanto", "nombre_comercial": "Acanto", "nombre_display": "Acanto",
    "municipio": "—", "departamento": "—", "potencia_instalada_kwp": null,
    "estado": "en_operacion", "project_id_solenium": "141" }
] }
```

### `GET /monitoreo/_legacy?action=getGeneration&sub_project={sub}&date_from={YYYY-MM-DD}&date_to={YYYY-MM-DD}`

Curva **horaria** de generación de una planta, en kWh, hora local Colombia (UTC-5), más la línea base P50/P90/P99 si el proyecto la tiene cargada.

```json
{ "ok": true,
  "data": [
    { "time": "2026-07-01 06:00", "date": "2026-07-01", "kwh": 4.32 },
    { "time": "2026-07-01 07:00", "date": "2026-07-01", "kwh": 16.8 }
  ],
  "simulation": { "p50_monthly": null, "p90_monthly": null, "p90_daily": null }
}
```

Para el total diario, agrupe por `date` y sume `kwh`. Los valores ya son **deltas por hora** (la plataforma resta lecturas consecutivas del contador acumulado), así que se suman directo.

### `GET /monitoreo/resumen-generacion?date_from=&date_to=`

Generación de **toda la flota** (~71 proyectos) en un rango, agregada por fecha y por proyecto. Una sola llamada en vez de 71.

```json
{ "projects_count": 71,
  "dates": [ { "fecha": "2026-07-01", "kwh_real": 123456.7 } ],
  "by_project": [ { "proyecto_id": 12, "nombre": "…", "sub_project": "…", "kwh_real": 9876.5 } ] }
```

Es el más lento de la sección: hace fan-out a la API de Unergy por cada proyecto. Rangos cortos, y cachear.

---

## 6. Endpoints de apoyo

| Endpoint | Para qué |
|---|---|
| `GET /asic?contrato_interno={nombre}` | Registros GESCON: qué planta está inscrita en qué contrato, con `porcentaje_despacho`, `fecha_inicio`, `fecha_fin_efectiva`, `es_duplicado`, `uso_del_recurso`. Filtros: `contrato_interno`, `codigo_sic_contrato`, `proyecto_id`. **No pagina** — devuelve todo lo que matchee |
| `GET /ppa` | Contratos PPA formales (partes, fechas, condiciones comerciales) |
| `GET /ppa/{id}` | Un contrato PPA en detalle |
| `GET /proyectos` | Catálogo de proyectos (paginado). Trae `sub_project`, potencia, estado, ubicación |
| `GET /cumplimiento/plantas-contratos?year=&month=` | Vista invertida: por planta, a qué contratos le despacha ese mes |
| `GET /cumplimiento/energia-transada?year=&month=` | Energía transada del período |

---

## 7. Cómo se calcula (para que los números cuadren)

1. **Compromisos** (`energia_minima_mwh` / `energia_maxima_mwh`) salen de la tabla de compromisos del PPA, cargados **por contrato y por mes**. No se derivan de nada: si nadie los cargó, llegan `null` y el `estado` es `sin_compromisos`.
2. **Generación por contrato** = Σ sobre las plantas inscritas en GESCON ese mes de `generación_de_la_planta × porcentaje_despacho`. Una planta puede estar en varios contratos con porcentajes distintos.
3. `porcentaje_despacho` se almacena como **fracción 0–1** (1.0 = 100%). Si lo va a mostrar, multiplique por 100.
4. **`es_duplicado: true`** = esa planta ya está comprometida en otro contrato; la energía se suministra igual, pero se cubre comprando en bolsa. Se reporta aparte en `exposicion_bolsa_duplicados_mwh`.
5. **`uso_del_recurso: true`** = cuenta como suministro normal, y `uso_recurso_mwh` estima lo que se le paga al cliente a precio de bolsa.
6. **Mantenimiento:** `energia_perdida_mantenimiento_mwh` se suma a la generación disponible para no penalizar downtime excusado — por eso existen `gen_disponible_mwh` y `compras_bolsa_ajustada_mwh` en paralelo a los campos normales.
7. **Valoración COP** usa el precio promedio de bolsa del mes (`valoracion_bolsa.precio_bolsa_avg_cop_kwh`). Si no hay precios cargados para ese mes, los campos `*_cop` llegan `null`.

---

## 8. Límites y cosas que hay que saber

**Latencia.** `/cumplimiento/ppa/resumen` dispara una consulta a la API de Unergy por cada planta (en paralelo, hasta 10 a la vez). Medido: ~6 s para julio 2026 con 22 contratos. Los endpoints anuales y `resumen-generacion` son bastante más lentos. Recomendaciones:
- No lo llame en un loop mes por mes sin cachear.
- Timeout del cliente en **90 s o más**, no en los 10 s por defecto de muchas librerías.
- Si solo necesita compromisos y no generación, use `/cumplimiento/ppa/resumen-anual` (solo BD, responde al instante).

**Esto es producción.** No hay staging. Todo lo de esta guía es de solo lectura, así que no hay riesgo de ensuciar datos — pero la carga sí la sienten los demás. Nada de barridos masivos en horario laboral.

**Sin paginación** en `/cumplimiento/*` ni en `/asic`: devuelven la lista completa. `/proyectos` sí pagina.

**Contratos sin compromiso cargado** llegan con `energia_minima_mwh: null` y `estado: "sin_compromisos"`. Hay que manejar ese caso: no es cero, es "no hay dato".

**Santa Fe 2 — dato sucio ya corregido (verificado 2026-08-13).** Este contrato tenía cargado un compromiso mín = máx de **180.000 MWh/mes** (1.080.000 MWh en 2026) contra una planta que genera ~192 MWh/mes: eran **kWh en un campo que espera MWh**, y contaminaban el bloque `totales` de `/cumplimiento/ppa/resumen` con un déficit artificial de ~178.795 MWh. **Ya está corregido**: hoy son **180 MWh/mes** (jul–dic 2026), y el contrato aparece como `excedente` porque genera ~192–210 MWh/mes contra un techo de 180.

Los `totales` ya son confiables. Julio 2026 cierra en 7.293,2 MWh de compromiso mínimo contra 8.658,0 MWh generados, `estado: "ok"`, sin compras en bolsa. Se deja anotado porque **cualquier análisis hecho antes del 2026-08-13 arrastra el déficit falso** — si aparece un déficit agregado de ~178.795 MWh, es este dato, no el negocio.

Regla general que sí sigue vigente: antes de graficar agregados, descarte compromisos con órdenes de magnitud imposibles. Ningún contrato de la compañía supera unos pocos miles de MWh/mes.

**`/proyectos/lista` y `/proyectos/buscar` no están desplegados** (responden 422). Use `/proyectos` o `/monitoreo/_legacy?action=getProjects`.

---

## 9. Receta completa en Python

```python
import os, requests

BASE = "https://operaciones.unergy.io/api/v1"
S = requests.Session()
S.headers["X-API-Key"] = os.environ["UNERGY_API_KEY"]   # nunca hardcodear la key

def cumplimiento(year, month):
    r = S.get(f"{BASE}/cumplimiento/ppa/resumen",
              params={"year": year, "month": month}, timeout=120)
    r.raise_for_status()
    return r.json()

d = cumplimiento(2026, 7)
print(f"Período {d['periodo']['year']}-{d['periodo']['month']:02d} "
      f"({d['periodo']['tipo_datos']}, datos hasta el día {d['periodo']['dia_max_datos']})")

for c in sorted(d["contratos"], key=lambda x: x["nombre_interno"] or ""):
    mn, mx, gen = c["energia_minima_mwh"], c["energia_maxima_mwh"], c["gen_total_mwh"]
    if mn is None and mx is None:
        print(f"{c['nombre_interno']:<28} sin compromisos cargados  (gen {gen:,.1f} MWh)")
        continue
    rango = f"{mn or 0:,.1f} – {mx:,.1f}" if mx is not None else f"≥ {mn:,.1f}"
    print(f"{c['nombre_interno']:<28} {rango:>22} MWh | gen {gen:>10,.1f} | {c['estado']}")
    if c["compras_bolsa_mwh"]:
        print(f"{'':<28} └─ faltan {c['compras_bolsa_mwh']:,.1f} MWh por bolsa")

# Curva horaria de una planta puntual
g = S.get(f"{BASE}/monitoreo/_legacy",
          params={"action": "getGeneration", "sub_project": "delta_1",
                  "date_from": "2026-07-01", "date_to": "2026-07-31"}, timeout=120).json()

por_dia = {}
for p in g["data"]:
    por_dia[p["date"]] = por_dia.get(p["date"], 0) + p["kwh"]
print(f"\ndelta_1 julio: {sum(por_dia.values())/1000:,.3f} MWh en {len(por_dia)} días")
```

---

## Resumen de una línea

**Denle solo la API Key de la plataforma.** `GET /cumplimiento/ppa/resumen?year&month` trae compromiso mínimo, máximo y generación de todos los contratos en una sola llamada; la API de Unergy la consume el backend por dentro y ella no necesita verla.
