# API del Panel de Cumplimiento — Plataforma Operaciones Unergy

Guía para reconstruir la gráfica y las tablas de la pestaña **Cumplimiento** de
`/mem/cumplimiento` en un panel propio.

- **Base URL:** `https://operaciones.unergy.io`
- **Sin Swagger interactivo.** La documentacion automatica la servia FastAPI en `/docs`, y desapareció con la migración a Django (2026-09-04). Esta guía es la referencia.
- **Formato:** JSON. Todo lo de esta guía es `GET` — **no escribe nada**.
- **Unidades:** energía siempre en **MWh**.

---

## Lo esencial en tres líneas

```bash
curl -H "X-API-Key: uop_xxxx..." \
  "https://operaciones.unergy.io/api/v1/cumplimiento/panel-anual?year=2026"
```

Esa **única llamada** trae todo: el consolidado de todos los contratos, cada contrato por
separado, los 12 meses de cada uno, y qué planta aportó cuánto. No hay que llamar nada más
ni calcular nada del lado del panel.

---

## 1. Autenticación

Header `X-API-Key` en cada request:

```bash
curl -H "X-API-Key: uop_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" ...
```

La key tiene formato `uop_` + 64 caracteres hex. **Pedísela a Juan José** — se emite desde
Admin → Usuarios → API Keys y solo se muestra una vez, al crearla.

> La key hereda los permisos del usuario al que se le emitió. Trátenla como una
> contraseña: no la pongan en código de frontend ni en un repositorio público. Si el panel
> es una app web, la llamada tiene que salir del **servidor**, no del navegador.

---

## 2. El endpoint

### `GET /api/v1/cumplimiento/panel-anual?year={YYYY}`

| Parámetro | Obligatorio | Default | Qué hace |
|---|---|---|---|
| `year` | **sí** | — | Año a consultar (2020–2050) |
| `incluir_plantas` | no | `true` | `false` quita el desglose planta por planta y aliviana bastante la respuesta |
| `refrescar` | no | `false` | `true` ignora la caché de 15 min y vuelve a consultar la generación |

---

## 3. La respuesta

```jsonc
{
  "year": 2026,
  "generado_en": "2026-08-07T16:20:00+00:00",
  "desde_cache": false,

  "consolidado": {
    "nombre": "Consolidado (todos)",
    "n_contratos": 22,
    "total_min_mwh": 18450.0,        // suma del año, todos los contratos
    "total_max_mwh": 24100.0,
    "meses_con_compromisos": 12,
    "meses": [ /* 12 objetos Mes */ ]
  },

  "contratos": [
    {
      "id": 18,
      "nombre_interno": "BIA Delta 1",
      "numero_codigo_contrato": "UNG-2026-018",
      "comprador_nombre": "BIA Energy",
      "fecha_inicio": "2026-01-01",
      "fecha_fin": "2030-12-31",

      "total_min_mwh": 1512.0,          // ← columna "Mín anual" de la tabla
      "total_max_mwh": null,            // ← columna "Máx anual"
      "meses_con_compromisos": 12,      // ← columna "Meses"

      "estado_cumplimiento": "cumple",  // cumple | no_cumple
      "meses_en_deficit": 0,
      "requiere_bolsa": false,
      "bolsa_anual_mwh": 0.0,

      "meses": [ /* 12 objetos Mes */ ]
    }
  ]
}
```

### El objeto `Mes`

```jsonc
{
  "month": 7,                            // 1–12

  "min_mwh": 126.0,                      // compromiso mínimo del mes (null = sin compromiso)
  "max_mwh": null,                       // compromiso máximo (null = SIN TOPE, no cero)

  "gen_mwh": 172.057,                    // generación real acumulada
  "gen_proyectada_mwh": null,            // proyección para meses futuros
  "gen_proyectada_cierre": null,         // proyección de cierre del mes en curso

  "valor_mwh": 172.057,                  // ⭐ EL QUE SE COMPARA CONTRA EL COMPROMISO

  "estado": "ok",
  "tipo_datos": "real",

  "dia_actual": null,                    // solo en el mes en curso
  "dias_restantes": null,

  "compras_bolsa_mwh": 0.0,              // cuánto falta para llegar al mínimo
  "excedentes_bolsa_mwh": 0.0,           // cuánto sobra por encima del máximo
  "exposicion_bolsa_duplicados_mwh": null,

  "n_plantas": 1,
  "plantas": [
    {
      "nombre": "GD Delta 1",
      "sub_project": "delta_1",
      "pct_despacho": 1.0,               // 0–1, NO 0–100
      "dias_en_contrato": 31,
      "dias_mes": 31,
      "gen_planta_mwh": 172.057,         // lo que generó la planta
      "gen_contrato_mwh": 172.057,       // lo que le tocó a ESTE contrato
      "es_duplicado": false              // true = compra en bolsa
    }
  ]
}
```

En el `consolidado`, cada `Mes` trae además:

| Campo | Qué es |
|---|---|
| `n_contratos_con_compromiso` | cuántos contratos tenían compromiso ese mes |
| `suma_compras_bolsa_mwh` | **la suma de los déficits de cada contrato** — ver §6 |
| `suma_excedentes_bolsa_mwh` | ídem con los excedentes |

Y en `plantas`, cada entrada trae `"contrato"` con el nombre del contrato al que aporta.

---

## 4. ⭐ El campo que importa: `valor_mwh`

**Para saber si un mes cumplió, comparen `valor_mwh` contra `min_mwh` y `max_mwh`.
No usen `gen_mwh` directamente.**

`gen_mwh` es solo la generación real acumulada. Pero un mes futuro todavía no generó nada,
y el mes en curso va a la mitad. Comparar `gen_mwh` contra el compromiso completo diría que
todos los meses futuros están en déficit, lo cual es falso.

`valor_mwh` ya resuelve eso: es la generación real en meses cerrados, la proyección de
cierre en el mes en curso, y la proyección basada en el promedio de los últimos 30 días en
meses futuros. Es exactamente el número que usa la plataforma. **Si lo usan, sus cifras van
a coincidir con las de Operaciones. Si lo calculan por su cuenta, no.**

`valor_mwh` puede venir `null` cuando el contrato no estaba vigente ese mes o cuando aún no
hay datos para evaluar. En ese caso, no dibujen barra.

---

## 5. Cómo se dibuja la gráfica

La gráfica es de barras, 12 meses en el eje X, MWh en el eje Y.

| Elemento visual | De dónde sale |
|---|---|
| **Banda verde** ("zona de cumplimiento") | rectángulo entre `min_mwh` y `max_mwh` |
| **Barra sólida** (generación) | altura = `valor_mwh` |
| **Barra punteada** (proyección) | cuando `tipo_datos != "real"`, misma altura, estilo distinto |
| **Línea roja** | `min_mwh` |
| **Línea morada** | `max_mwh` |
| **Sombra roja** sobre la barra | cuando `estado == "deficit"`: el tramo entre `valor_mwh` y `min_mwh` |
| **Sombra turquesa** | cuando `estado == "excedente"`: el tramo entre `max_mwh` y `valor_mwh` |
| **Tooltip** | `valor_mwh`, `min_mwh`, `max_mwh`, y `compras_bolsa_mwh` o `excedentes_bolsa_mwh` |
| **Modal al hacer clic** | la tabla `plantas` de ese mes |

Colores de la plataforma, por si quieren que se vea igual:

| | |
|---|---|
| Generación real | `#915BD8` (morado) |
| Proyección | `rgba(59,186,220,0.65)` (turquesa claro, punteado) |
| Zona de cumplimiento | `rgba(46,125,50,0.10)` (verde) |
| Déficit | `#D64455` (rojo) |
| Excedente | `#14B8A6` (turquesa) |

### Los estados

| `estado` | Significa | Cómo pintarlo |
|---|---|---|
| `ok` | entre el mínimo y el máximo | normal |
| `deficit` | por debajo del mínimo | sombra roja |
| `excedente` | por encima del máximo | sombra turquesa |
| `sin_compromisos` | el contrato no tiene compromiso ese mes | barra sin banda |
| `sin_datos` | hay compromiso pero aún no hay con qué evaluarlo | barra gris o vacía |
| `finalizado` | el contrato ya terminó | sin barra |
| `no_iniciado` | el contrato aún no empieza | sin barra |

**Traten `sin_compromisos`, `sin_datos`, `finalizado` y `no_iniciado` como "no evaluable",
no como incumplimiento.**

### `tipo_datos`

| Valor | Qué es |
|---|---|
| `real` | mes cerrado, dato definitivo |
| `mes_actual` | mes en curso; usen también `dia_actual` y `dias_restantes` |
| `proyeccion_historica` | mes futuro, proyectado sobre el promedio de los últimos 30 días |

---

## 6. ⚠️ Dos números de déficit, y no son intercambiables

En el `consolidado`, cada mes trae dos cifras que parecen lo mismo:

- **`compras_bolsa_mwh`** — el déficit **del agregado**. Se calcula sumando todos los
  contratos y comparando ese total contra la suma de los mínimos. **Es lo que dibuja la
  gráfica consolidada.**
- **`suma_compras_bolsa_mwh`** — la **suma de los déficits contrato por contrato**.

Casi nunca dan igual, y la diferencia importa. Ejemplo real:

> El contrato A quedó 30 MWh corto de su mínimo. El contrato B generó 30 MWh de más.
> En el agregado se compensan y `compras_bolsa_mwh` da **0**.
> Pero los contratos **no se netean entre sí**: el excedente de B no le sirve a A. Hay que
> comprar esos 30 MWh en bolsa igual. `suma_compras_bolsa_mwh` da **30**.

**Para la gráfica, usen `compras_bolsa_mwh`** (es lo que muestra la plataforma).
**Para responder "cuánta energía hay que comprar en bolsa", usen `suma_compras_bolsa_mwh`.**

---

## 7. Límites y cosas que hay que saber

**Latencia.** La primera llamada del año tarda: el backend consulta la generación real de
cada planta y cada mes contra la API de Unergy. Después queda **cacheada 15 minutos** y
responde al instante.

- Pongan el **timeout del cliente en 120 s o más**, no en los 10 s que traen muchas
  librerías por defecto.
- **No la llamen en cada render.** Tráiganla una vez al cargar el panel y guárdenla en
  estado.
- `?incluir_plantas=false` aliviana mucho la respuesta si no van a mostrar el desglose.
- `?refrescar=true` solo si el usuario aprieta "actualizar". No en cada carga.

**Esto es producción y no hay staging.** Todo lo de esta guía es de solo lectura, así que
no hay riesgo de ensuciar datos, pero la carga sí la sienten los demás. Nada de barridos
masivos de varios años en horario laboral.

**El endpoint cubre contratos de venta.** Los contratos de compra (`tipo_contrato =
"compra"`) quedan fuera, igual que en la pestaña de la plataforma.

**`max_mwh: null` significa "sin tope", no "tope en cero".** Un contrato sin máximo nunca
está en excedente. Si lo tratan como 0, todo va a salir en excedente.

**`pct_despacho` viene entre 0 y 1**, no entre 0 y 100. Para mostrarlo como porcentaje,
multiplíquenlo por 100.

**Dos campos parecidos en cada contrato.** `total_min_mwh` (de la tabla) es *null-aware*:
si ningún mes tiene compromiso, da `null`. `total_min_anual_mwh` (del rollup) trata los
nulls como cero y siempre da un número. **Para la tabla usen `total_min_mwh`.**

**⚠️ Dato sucio conocido — Santa Fe 2.** Ese contrato tiene compromiso mín = máx =
**180.000 MWh/mes**, contra una planta que genera ~192 MWh/mes. Es ~50× el contrato más
grande de la compañía; casi con seguridad se cargaron kWh en un campo que espera MWh.
**Mientras no se corrija, contamina el `consolidado`.** Si el panel va a mostrar el total,
o excluyen ese contrato (`id` en el arreglo `contratos`) o se corrige el dato primero.
Los contratos individuales sí están bien. Pregúntenle a Juan José por el estado de esto.

---

## 8. Receta completa en Python

```python
import os
import requests

BASE = "https://operaciones.unergy.io/api/v1"
KEY = os.environ["UNERGY_API_KEY"]          # nunca hardcodeada

def panel_anual(year: int, incluir_plantas: bool = True) -> dict:
    r = requests.get(
        f"{BASE}/cumplimiento/panel-anual",
        params={"year": year, "incluir_plantas": incluir_plantas},
        headers={"X-API-Key": KEY},
        timeout=180,                         # la primera llamada es lenta
    )
    r.raise_for_status()
    return r.json()


data = panel_anual(2026)

# ── La serie que dibuja la gráfica consolidada
for m in data["consolidado"]["meses"]:
    print(
        f"{m['month']:>2}  "
        f"min={m['min_mwh'] or '—':>10}  "
        f"max={m['max_mwh'] or 'sin tope':>10}  "
        f"valor={m['valor_mwh'] or '—':>10}  "
        f"{m['estado']}"
    )

# ── La tabla "Resumen anual por contrato"
for c in data["contratos"]:
    print(f"{c['nombre_interno']:<28} {c['comprador_nombre']:<20} "
          f"mín={c['total_min_mwh']}  máx={c['total_max_mwh']}  "
          f"{c['meses_con_compromisos']}/12")

# ── Cuánta energía hay que comprar en bolsa en el año (número operativo real)
a_comprar = sum(m["suma_compras_bolsa_mwh"] or 0 for m in data["consolidado"]["meses"])
print(f"\nCompras en bolsa proyectadas {data['year']}: {a_comprar:,.1f} MWh")
```

### En JavaScript

```js
const BASE = "https://operaciones.unergy.io/api/v1";

// OJO: esto va del lado del SERVIDOR. La API key no puede llegar al navegador.
async function panelAnual(year) {
  const r = await fetch(`${BASE}/cumplimiento/panel-anual?year=${year}`, {
    headers: { "X-API-Key": process.env.UNERGY_API_KEY },
    signal: AbortSignal.timeout(180_000),
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}

const data = await panelAnual(2026);

const serie = data.consolidado.meses.map((m) => ({
  mes: m.month,
  min: m.min_mwh,
  max: m.max_mwh,
  valor: m.valor_mwh,            // la altura de la barra
  esProyeccion: m.tipo_datos !== "real",
  estado: m.estado,
}));
```

---

## 9. Errores

| Código | Qué pasó |
|---|---|
| `401` | la API key está mal, inactiva, o el usuario fue desactivado |
| `422` | falta `year`, o está fuera del rango 2020–2050 |
| `500` | error nuestro — avísennos con la hora y el `year` que pidieron |

Si la generación no se pudo traer (la API de Unergy no respondió), el endpoint **no falla**:
devuelve los compromisos con `gen_mwh` en 0 y `valor_mwh` en `null`. Por eso conviene
mostrar "sin datos" en vez de "0 MWh" cuando `valor_mwh` viene `null`.

---

## Resumen de una línea

Una llamada a `GET /cumplimiento/panel-anual?year=2026` con el header `X-API-Key`, y
dibujen `valor_mwh` contra la banda `min_mwh`–`max_mwh`. Eso es todo.
