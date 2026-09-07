# API de Proyectos — Plataforma Operaciones Unergy

Guía para consultar los proyectos de la plataforma de forma programática.

- **Base URL:** `https://operaciones.unergy.io`
- **Prefijo:** todos los endpoints viven bajo `/api/v1`
- **Sin Swagger interactivo.** La documentacion automatica la servia FastAPI en `/docs`, y desapareció con la migración a Django (2026-09-04). Esta guía es la referencia.
- **Formato:** JSON en las respuestas

Los tres endpoints de esta guía son de **solo lectura**: no modifican nada, así que
podés llamarlos con confianza contra producción.

---

## El flujo en tres pasos

```
1. GET /api/v1/proyectos/lista        →  todos los proyectos, con su id
2. elegís el id que te interesa
3. GET /api/v1/proyectos/{id}         →  el detalle completo de ese proyecto
```

Si ya sabés cómo se llama el proyecto, podés saltarte los pasos 1 y 2:

```
GET /api/v1/proyectos/buscar?nombre=Marimonda   →  el detalle completo, directo
```

---

## 1. Autenticación

Todos los endpoints exigen autenticación. Hay dos formas.

### API Key (recomendado para scripts)

Header `X-API-Key` en cada request:

```bash
curl https://operaciones.unergy.io/api/v1/proyectos/lista \
  -H "X-API-Key: uop_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

La key tiene formato `uop_` + 64 caracteres hex. **Pedísela a Juan José** — se emite
desde Admin → Usuarios → API Keys y solo se muestra una vez, al crearla.

Verificá que funciona:

```bash
curl https://operaciones.unergy.io/api/v1/api-keys/verify \
  -H "X-API-Key: $UNERGY_API_KEY"
# → {"user_id": 3, "nombre": "...", "email": "...", "rol": "operaciones"}
```

> **Ojo con los permisos:** la API Key hereda **todos** los permisos del usuario al
> que está asociada — incluidos los de escritura. El campo `scopes` se guarda pero hoy
> no se valida. Tratala como una contraseña: en variable de entorno, nunca en el repo,
> nunca en un frontend.

### Bearer token (alternativa)

```bash
curl -X POST https://operaciones.unergy.io/api/v1/auth/token \
  -d "username=correo@unergy.io&password=tu-contraseña"
# → {"access_token": "eyJ..."}
```

Después mandá `Authorization: Bearer eyJ...` en cada request. Ojo: el body del login
es `application/x-www-form-urlencoded`, no JSON.

### Errores de autenticación

| Código | Causa |
|---|---|
| 401 `Token requerido` | No mandaste ni `X-API-Key` ni `Authorization` |
| 401 `API Key inválida` | La key no existe o está desactivada |
| 401 `Token inválido` | El JWT venció o está mal formado |
| 401 `Usuario inactivo o no encontrado` | El usuario dueño de la credencial está desactivado |

---

## 2. `GET /proyectos/lista` — todos los proyectos

Devuelve **todos** los proyectos vigentes en una sola llamada, sin paginar, con los
campos justos para identificarlos y quedarte con el `id`.

```bash
curl https://operaciones.unergy.io/api/v1/proyectos/lista \
  -H "X-API-Key: $UNERGY_API_KEY"
```

Respuesta:

```json
{
  "total": 87,
  "items": [
    {
      "id": 12,
      "nombre_comercial": "Minigranja 0029 - Monterrubio",
      "estado": "en_operacion",
      "tipo_proyecto": "minigranja",
      "municipio": "Monterrubio",
      "departamento": "Sucre",
      "potencia_instalada_kwp": 990.0,
      "sub_project": "monterrubio",
      "codigo_tsf": "MGS-0029"
    }
  ]
}
```

| Campo | Tipo | Notas |
|---|---|---|
| `id` | entero | **Es el que usás para pedir el detalle** |
| `nombre_comercial` | texto | El nombre principal del proyecto |
| `estado` | texto | `en_desarrollo`, `en_operacion`, `suspendido` o `cancelado` |
| `tipo_proyecto` | texto o `null` | `minigranja`, `autoconsumo`, `gd`, `movilidad_electrica`, `otro` |
| `municipio`, `departamento` | texto o `null` | Ubicación |
| `potencia_instalada_kwp` | número o `null` | Potencia instalada en kWp |
| `sub_project` | texto o `null` | El *API ID Unergy* del proyecto |
| `codigo_tsf` | texto o `null` | Código en Sun Factory |

Detalles del comportamiento:

- Viene **ordenado por `nombre_comercial`**.
- **Excluye los proyectos borrados** (borrado lógico, `deleted_at`).
- No trae relaciones anidadas a propósito: para eso es el paso del detalle.
- No acepta filtros ni paginación. Si necesitás filtrar por estado, tipo, portafolio o
  servicio, usá `GET /api/v1/proyectos` (ver la sección 5).

---

## 3. `GET /proyectos/{id}` — detalle de un proyecto

Tomá el `id` del listado y pedí el detalle completo:

```bash
curl https://operaciones.unergy.io/api/v1/proyectos/12 \
  -H "X-API-Key: $UNERGY_API_KEY"
```

Devuelve un objeto grande (~60 campos escalares más varias relaciones). Recortado:

```json
{
  "id": 12,
  "nombre_comercial": "Minigranja 0029 - Monterrubio",
  "nombre_bitacora": "Monterrubio",
  "nombre_clientes": null,
  "estado": "en_operacion",
  "tipo_proyecto": "minigranja",
  "clasificacion_regulatoria": "AGGE",
  "tipo_tecnologia": "solar",
  "potencia_instalada_kwp": 990.0,
  "municipio": "Monterrubio",
  "departamento": "Sucre",
  "latitud": 9.245812,
  "longitud": -75.331904,
  "operador_red": "AFINIA",
  "fecha_entrada_operacion": "2024-11-15",
  "fecha_inicio_comercializacion": "2024-12-01",
  "codigo_sic_generacion": "FRTXXXXX",
  "sub_project": "monterrubio",
  "codigo_tsf": "MGS-0029",
  "srv_operacion": true,
  "srv_representacion": true,
  "srv_cgm": false,
  "srv_ppa": true,
  "srv_promotor": false,
  "srv_rec": false,
  "info_tecnica": { "potencia_ac_kw": 990.0, "marca_inversores": "Huawei" },
  "inversionistas": [
    { "cliente_id": 44, "cliente_nombre": "Fondo XYZ", "porcentaje_participacion": 0.6 }
  ],
  "inversores": [
    { "id": 301, "nombre": "Inversor 1", "potencia_nominal_kw": 300.0 }
  ],
  "area_contactos": [],
  "servicio_representacion": null,
  "created_at": "2024-08-01T14:22:10Z",
  "updated_at": "2026-07-30T09:11:02Z"
}
```

Grupos de información que trae:

| Bloque | Qué contiene |
|---|---|
| Identificación | `nombre_comercial`, `nombre_bitacora`, `nombre_clientes`, `topic_slug`, `sub_project`, `codigo_tsf`, `origina_code`, `codigo_cnd` |
| Clasificación | `estado`, `tipo_proyecto`, `tipo_tecnologia`, `clasificacion_regulatoria` |
| Capacidad | `potencia_instalada_kwp`, `potencia_con_cen_mw`, `cantidad_total_paneles`, `produccion_especifica_kwh_kwp`, y las series `p50_mensual_kwh` / `p90_mensual_kwh` / `p99_mensual_kwh` |
| Ubicación | `departamento`, `municipio`, `direccion_vereda`, `latitud`, `longitud`, `operador_red`, `tipo_conexion` |
| Fechas | `fecha_entrada_operacion`, `fecha_inicio_comercializacion`, `fecha_fin_representacion`, `fecha_estimada_energizacion` |
| Códigos de mercado | Los IDs de Quoia y `project_id_solenium` (los códigos SIC de generación/consumo viven en la API de Liquidaciones, ver `/liquidaciones-api/proyectos/{id}`) |
| Servicios contratados | Las banderas `srv_operacion`, `srv_representacion`, `srv_cgm`, `srv_ppa`, `srv_promotor`, `srv_rec` |
| Relaciones | `info_tecnica`, `inversionistas`, `inversores`, `area_contactos`, `servicio_representacion` |

**404** si el `id` no existe: `{"detail": "Proyecto no encontrado"}`

---

## 4. `GET /proyectos/buscar?nombre=X` — detalle por nombre

El atajo para cuando ya sabés el nombre. Devuelve **exactamente la misma estructura**
que `GET /proyectos/{id}`.

```bash
curl -G https://operaciones.unergy.io/api/v1/proyectos/buscar \
  --data-urlencode "nombre=Minigranja 0029 - Monterrubio" \
  -H "X-API-Key: $UNERGY_API_KEY"
```

> Usá `-G --data-urlencode` (o codificá la URL a mano) porque el nombre lleva espacios.

### Qué tolera y qué no

El match es **exacto pero normalizado**: ignora mayúsculas, tildes, guiones y espacios
de más. Todas estas resuelven al mismo proyecto:

```
Minigranja 0029 - Monterrubio      ✅
minigranja 0029 monterrubio        ✅
MINIGRANJA 0029 - MONTERRUBIO      ✅
Minigranja  0029  –  Monterrúbio   ✅
  Minigranja 0029 - Monterrubio    ✅  (los espacios de los bordes se ignoran)
```

Lo que **no** hace es adivinar. Tiene que ser el nombre completo:

```
monterrubio                        ❌ 404 — es un nombre parcial
Monterubio                         ❌ 404 — está mal escrito
0029 Monterrubio Minigranja        ❌ 404 — las palabras van en orden
```

Es deliberado: preferimos darte un error a devolverte en silencio el proyecto
equivocado. Si no sabés el nombre exacto, traé la lista con `/proyectos/lista` y
buscá ahí.

### Qué campos busca

1. Primero `nombre_comercial`.
2. Solo si ahí no hubo ninguna coincidencia, prueba con `nombre_bitacora` y
   `nombre_clientes`.

Una coincidencia por `nombre_comercial` siempre gana; el segundo paso no le suma
candidatos. Los códigos (`sub_project`, `codigo_tsf`, etc.) **no** entran en esta
búsqueda: son identificadores, no nombres, y ya vienen en el listado.

### Errores

**404 — ningún proyecto coincide:**

```json
{
  "detail": "No existe un proyecto cuyo nombre coincida con 'monterrubio'. Consultá GET /api/v1/proyectos/lista para ver los nombres disponibles."
}
```

**409 — el nombre coincide con más de un proyecto.** Pasa de verdad: `nombre_comercial`
no es único en la base y hay duplicados históricos. En vez de elegir por vos, te
devuelve los candidatos para que pidas el detalle por `id`:

```json
{
  "detail": {
    "mensaje": "Hay 2 proyectos cuyo nombre coincide con 'Chinú Sur'. Consultá el detalle por ID.",
    "nombre_ambiguo": true,
    "candidatos": [
      { "id": 41, "nombre_comercial": "Chinú Sur" },
      { "id": 58, "nombre_comercial": "Chinu Sur" }
    ]
  }
}
```

**422 — falta el parámetro `nombre`** o vino vacío. Lo valida FastAPI.

---

## 5. Si necesitás filtrar: `GET /proyectos`

El endpoint que usa el frontend. Trae el objeto **completo** de cada proyecto (con
todas las relaciones anidadas), paginado, y acepta filtros:

```bash
curl -G https://operaciones.unergy.io/api/v1/proyectos \
  -d "estado=en_operacion" -d "tipo_proyecto=minigranja" -d "size=100" \
  -H "X-API-Key: $UNERGY_API_KEY"
```

| Parámetro | Valores |
|---|---|
| `page` | Página, desde 1 (default 1) |
| `size` | Filas por página, 1–500 (default 20) |
| `q` | Búsqueda parcial en `nombre_comercial` (tipo "contiene", sin normalizar tildes) |
| `estado` | `en_desarrollo`, `en_operacion`, `suspendido`, `cancelado` |
| `tipo_proyecto` | `minigranja`, `autoconsumo`, `gd`, `movilidad_electrica`, `otro` |
| `portafolio_id` | Entero |
| `servicio` | `operacion`, `representacion`, `cgm`, `ppa`, `promotor`, `rec` — banderas de servicio contratado (`srv_*`), no el contrato PPA real |
| `ppa_id` | Entero, repetible (`?ppa_id=12&ppa_id=45`). Proyectos vinculados a alguno de esos contratos (tabla `ppa_contratos`, join por `ppa_contrato_proyectos`) |
| `sin_ppa` | `true` — proyectos sin ningún contrato PPA vinculado. Se combina con `ppa_id` como OR: `ppa_id=12&sin_ppa=true` trae los del contrato 12 más los que no tienen ninguno |

Respuesta: `{"items": [...], "total": N, "page": 1, "size": 20, "pages": 5}`

Es mucho más pesado que `/proyectos/lista`, así que usalo solo cuando de verdad
necesites filtrar o ya quieras los datos completos de varios proyectos a la vez.

---

## 6. Ejemplo completo

### bash

```bash
export UNERGY_API_KEY="uop_..."
BASE="https://operaciones.unergy.io/api/v1"

# 1. Buscar el id del proyecto por su nombre en el listado
ID=$(curl -s "$BASE/proyectos/lista" -H "X-API-Key: $UNERGY_API_KEY" \
     | jq -r '.items[] | select(.nombre_comercial | test("Monterrubio")) | .id')

# 2. Traer el detalle
curl -s "$BASE/proyectos/$ID" -H "X-API-Key: $UNERGY_API_KEY" | jq '.'
```

### Python

```python
import os
import requests

BASE = "https://operaciones.unergy.io/api/v1"
SESION = requests.Session()
SESION.headers["X-API-Key"] = os.environ["UNERGY_API_KEY"]


def listar_proyectos():
    r = SESION.get(f"{BASE}/proyectos/lista", timeout=60)
    r.raise_for_status()
    return r.json()["items"]


def detalle(proyecto_id: int):
    r = SESION.get(f"{BASE}/proyectos/{proyecto_id}", timeout=60)
    r.raise_for_status()
    return r.json()


def detalle_por_nombre(nombre: str):
    r = SESION.get(f"{BASE}/proyectos/buscar", params={"nombre": nombre}, timeout=60)
    if r.status_code == 404:
        raise LookupError(r.json()["detail"])
    if r.status_code == 409:
        d = r.json()["detail"]
        opciones = ", ".join(f"{c['nombre_comercial']} (id {c['id']})" for c in d["candidatos"])
        raise LookupError(f"{d['mensaje']} Candidatos: {opciones}")
    r.raise_for_status()
    return r.json()


# Flujo típico: listar, elegir, pedir el detalle
proyectos = listar_proyectos()
print(f"{len(proyectos)} proyectos")

operando = [p for p in proyectos if p["estado"] == "en_operacion"]
for p in operando[:5]:
    d = detalle(p["id"])
    print(p["nombre_comercial"], "→", d["potencia_instalada_kwp"], "kWp")
```

---

## Referencia rápida

| Método | Ruta | Para qué |
|---|---|---|
| GET | `/api/v1/proyectos/lista` | Todos los proyectos, campos livianos, una llamada |
| GET | `/api/v1/proyectos/{id}` | Detalle completo por ID |
| GET | `/api/v1/proyectos/buscar?nombre=X` | Detalle completo por nombre |
| GET | `/api/v1/proyectos` | Listado completo con filtros y paginación |

| Código | Significado |
|---|---|
| 200 | OK |
| 401 | Credencial faltante, inválida o usuario inactivo |
| 404 | El ID no existe, o ningún nombre coincide |
| 409 | El nombre coincide con varios proyectos (te devuelve los candidatos) |
| 422 | Falta un parámetro requerido, o el tipo es incorrecto |
