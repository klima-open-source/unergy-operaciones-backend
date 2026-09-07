# API · Vista de contratos

Responde, en **una sola llamada**, la pregunta operativa de todos los días:

> ¿En qué contrato está cada planta el 20 de agosto, cuánto se comprometió ese
> contrato ese mes, y cuánto genera cada planta en un mes típico?

No hace falta entender GESCON, vigencias ni piscinas: el backend ya lo resolvió.

- **URL:** `https://operaciones.unergy.io/api/v1/cumplimiento/vista-contratos`
- **Método:** `GET`. Es de **solo lectura**: no escribe nada, se puede llamar sin miedo.
- **Sin Swagger interactivo.** La documentacion automatica la servia FastAPI en `/docs`, y desapareció con la migración a Django (2026-09-04). Esta guía es la referencia.

---

## 1. Autenticación

Header `X-API-Key` en cada llamada:

```bash
curl -H "X-API-Key: uop_TU_KEY" \
  "https://operaciones.unergy.io/api/v1/cumplimiento/vista-contratos?fecha=2026-08-20"
```

La key la genera un admin de la plataforma. **Se muestra una sola vez, al
crearla** — el listado solo guarda los primeros 12 caracteres. Una key completa
mide 68 (`uop_` + 64). Si copiás del listado vas a tener 12 y siempre dará 401.

Si no tenés key, sirve un token de sesión:

```bash
curl -X POST "https://operaciones.unergy.io/api/v1/auth/token" \
  -d "username=tu@unergy.io&password=TU_CLAVE"
# devuelve {"access_token": "eyJ..."} → usalo como  -H "Authorization: Bearer eyJ..."
```

---

## 2. Parámetros

| Parámetro | Obligatorio | Default | Qué hace |
|---|---|---|---|
| `fecha` | **sí** | — | El día de la foto, `YYYY-MM-DD`. Ver §3. |
| `responsable` | no | `Unergy` | Empresa responsable a mostrar. `todos` o vacío = sin filtro. Ver §4. |
| `incluir_todos` | no | `false` | Incluir contratos de responsables marcados como no relevantes. |

```
?fecha=2026-08-20                      → los de Unergy ese día
?fecha=2026-08-20&responsable=todos    → todos los responsables
?fecha=2026-09-01&responsable=Externo  → otro día, otro responsable
```

---

## 3. Es la foto de UN día, no del mes

Esto es lo que más se malinterpreta.

Una planta aparece **solo si su vínculo con el contrato cubre ese día exacto**.
Una que salió el 19 no sale el 20, y una que entra el 21 tampoco — aunque las dos
hayan estado en el contrato buena parte de agosto.

Las que se mueven dentro del mes vienen marcadas: `"marcas": ["entra el 12"]`,
`["sale el 25"]`. Sirve para ver qué cambió sin comparar dos consultas.

Los compromisos (`min_mes_mwh`, `max_mes_mwh`) sí son **del mes** de esa fecha:
son mensuales por naturaleza.

---

## 4. El filtro de responsable es estricto

`responsable=Unergy` deja **solo** los contratos cuya empresa responsable es
Unergy. Un contrato **sin responsable asignado tampoco pasa** — es una decisión
deliberada: pedir "los de Unergy" y recibir además los que nadie asignó daría un
universo distinto al pedido.

Nada desaparece en silencio: lo que quedó fuera viene en `excluidos`, con nombre
y motivo. Si un total no cuadra, mirá ahí primero.

---

## 5. La respuesta

```json
{
  "fecha": "2026-08-20",
  "responsable": "Unergy",
  "mes_consultado": "2026-08",
  "totales": {
    "n_contratos": 12,
    "n_plantas": 46,
    "min_mes_mwh": 7304.6,
    "gen_prom_total_mwh": 8120.4,
    "contratos_sin_minimo": 1,
    "plantas_sin_promedio": 0
  },
  "contratos": [
    {
      "contrato_id": 1,
      "contrato": "Terpel 1 (Ayurá 1)",
      "codigo": "UNERGY 001-2023",
      "comprador": "TERPEL ENERGÍA S.A.S. E.S.P.",
      "responsable": "Unergy",
      "portafolio": "Ayurá",
      "min_mes_mwh": 1345.0,
      "max_mes_mwh": 2201.0,
      "gen_prom_total_mwh": 1863.0,
      "estado": "ok",
      "n_plantas": 12,
      "plantas_sin_promedio": 0,
      "plantas": [
        {
          "proyecto_id": 7,
          "planta": "MGS 0004 Valle de Gandalf",
          "fpo": "2024-02-22",
          "gen_prom_mwh_mes": 213.3,
          "gen_prom_origen": "api",
          "pct_asignado": 1.0,
          "codigo_sic": "89115",
          "desde": "2026-08-01",
          "hasta": "2026-08-31",
          "marcas": []
        }
      ]
    }
  ],
  "excluidos": [
    {"contrato": "BIA Naos 1", "responsable": "Externo", "n_plantas": 1, "motivo": "responsable"}
  ],
  "avisos": ["Lumina: sin plantas asignadas el 2026-08-20"]
}
```

### Los campos, uno por uno

**Del contrato**

| Campo | Qué es |
|---|---|
| `min_mes_mwh` / `max_mes_mwh` | Compromiso mínimo y máximo del contrato **para ese mes**, en MWh. `null` = no hay compromiso cargado — **no es cero** |
| `gen_prom_total_mwh` | Suma de los promedios de sus plantas, ponderada por el % de despacho. `null` si a alguna planta le falta el promedio (ver abajo) |
| `estado` | `ok` · `deficit` · `excedente` · `sin_compromisos` · `sin_datos` |
| `portafolio` | Portafolio dominante de las plantas. `Ayurá (+1)` = hay plantas de más de un portafolio |

**De la planta**

| Campo | Qué es |
|---|---|
| `gen_prom_mwh_mes` | Cuánto genera en un **mes típico** (MWh): promedio de los últimos 30 días corridos, calculado aparte y guardado en la base |
| `gen_prom_origen` | `api` = derivado del histórico · `manual` = lo cargó una persona (plantas sin histórico) |
| `pct_asignado` | Fracción **0–1** que despacha a ese contrato. `0.5` = 50%. Multiplicá por 100 para mostrarlo |
| `fpo` | Fecha de entrada en operación |
| `desde` / `hasta` | Ventana de la planta en ese contrato, dentro del mes |
| `marcas` | Etiquetas: `duplicada`, `uso del recurso`, `% dudoso`, `falta promedio`, `entra el 12`, `sale el 25` |

---

## 6. Lo que la respuesta dice en voz alta

Un hueco callado se lee como "no había nada", que es peor que un error visible.

| Caso | Qué llega |
|---|---|
| Contrato sin compromiso cargado | `min_mes_mwh: null` y `estado: "sin_compromisos"`. **No** un `0` |
| Planta sin promedio calculado | `gen_prom_mwh_mes: null` + marca `falta promedio`, y `totales.plantas_sin_promedio` los cuenta |
| Contrato al que le falta el promedio de alguna planta | `gen_prom_total_mwh: null` y `estado: "sin_datos"`. Sumar a medias daría un déficit falso |
| Contrato vigente pero sin plantas ese día | aparece con `n_plantas: 0` + una línea en `avisos` |
| Contrato de otro responsable | no está en `contratos`, sí en `excluidos` |

**Si ves muchos `plantas_sin_promedio`**, el promedio todavía no se ha calculado
para esas plantas. Se resuelve corriendo, una vez:

```
POST /api/v1/proyectos/gen-promedio/recalcular?dias=30&dry_run=false
```

(`dry_run=true`, el default, solo reporta.) Las plantas sin histórico se cargan a
mano con `PATCH /proyectos/{id}` mandando `gen_mensual_promedio_mwh`; eso las
marca `manual` y el recálculo ya no las pisa.

---

## 7. Recetas

### Python

```python
import requests

BASE = "https://operaciones.unergy.io/api/v1"
S = requests.Session()
S.headers["X-API-Key"] = "uop_TU_KEY"      # nunca la pegues en un repo

d = S.get(f"{BASE}/cumplimiento/vista-contratos",
          params={"fecha": "2026-08-20"}, timeout=120).json()

print(f"{d['fecha']} · {d['totales']['n_contratos']} contratos, "
      f"{d['totales']['n_plantas']} plantas")

for c in d["contratos"]:
    mn = f"{c['min_mes_mwh']:,.1f}" if c["min_mes_mwh"] is not None else "sin mínimo"
    print(f"\n{c['contrato']}  ({c['portafolio']})  mín {mn} MWh  → {c['estado']}")
    for p in c["plantas"]:
        gen = f"{p['gen_prom_mwh_mes']:,.1f}" if p["gen_prom_mwh_mes"] is not None else "—"
        print(f"   {p['planta']:<38} {gen:>9} MWh/mes  {p['pct_asignado']:.0%}")

for e in d["excluidos"]:
    print(f"fuera por responsable: {e['contrato']} ({e['responsable']})")
```

### Pasarlo a Excel

```python
import pandas as pd

filas = [
    {"contrato": c["contrato"], "portafolio": c["portafolio"],
     "min_mes_mwh": c["min_mes_mwh"], "max_mes_mwh": c["max_mes_mwh"],
     "estado": c["estado"], "planta": p["planta"], "fpo": p["fpo"],
     "gen_prom_mwh_mes": p["gen_prom_mwh_mes"],
     "pct_asignado": p["pct_asignado"], "marcas": ", ".join(p["marcas"])}
    for c in d["contratos"] for p in c["plantas"]
]
pd.DataFrame(filas).to_excel("contratos_2026-08-20.xlsx", index=False)
```

### Power BI / Power Query

`Origen → Web → Avanzadas`, URL:

```
https://operaciones.unergy.io/api/v1/cumplimiento/vista-contratos?fecha=2026-08-20
```

Encabezado: `X-API-Key` = tu key. Después expandí `contratos` y dentro `plantas`.

### Google Sheets

```javascript
function vistaContratos(fecha) {
  const r = UrlFetchApp.fetch(
    "https://operaciones.unergy.io/api/v1/cumplimiento/vista-contratos?fecha=" + fecha,
    {headers: {"X-API-Key": "uop_TU_KEY"}});
  const d = JSON.parse(r.getContentText());
  const filas = [["Contrato","Portafolio","Min_Mes","Max_mes","Estado","Planta","FPO","Gen_prom","%"]];
  d.contratos.forEach(c => c.plantas.forEach(p => filas.push(
    [c.contrato, c.portafolio, c.min_mes_mwh, c.max_mes_mwh, c.estado,
     p.planta, p.fpo, p.gen_prom_mwh_mes, p.pct_asignado])));
  return filas;
}
```

---

## 8. Errores

| Código | Qué pasó | Qué hacer |
|---|---|---|
| `401` | Credencial inválida, vencida o revocada | El cuerpo trae el motivo exacto en `detail`. Si dice `API Key inválida`, pedí una nueva |
| `422` | `fecha` mal escrita | Usá `YYYY-MM-DD`: `2026-08-20`, no `20/08/2026` |
| `500` | Algo se rompió del lado del servidor | Avisá con la hora y la URL exacta |

---

## 9. Cosas que conviene saber

- **Tarda unos segundos.** Resuelve las vigencias GESCON de todo el mes. Poné el
  timeout del cliente en 120 s, no en los 10 s de muchas librerías.
- **No pagina**: devuelve todo. Son decenas de contratos, no miles.
- **Es producción.** Solo lectura, así que no hay riesgo de ensuciar datos, pero
  no la llames en un bucle cerrado: cacheá el resultado del día.
- **`pct_asignado` es 0–1.** Un `1.0` es 100%.
- Los MWh de `gen_prom_mwh_mes` son un **promedio**, no energía facturada. No
  sirven para liquidar.
