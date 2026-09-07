# API del pipeline de PPAs

Una llamada devuelve **los contratos de energía del pipeline comercial**, ordenados como
un árbol:

**PPA → PROYECTOS → detalles de cada proyecto.**

- **Base URL:** `https://backend-production-63d8.up.railway.app`
- **Endpoint:** `GET /api/v1/comercial/proyectos-operando`
- **Swagger:** https://backend-production-63d8.up.railway.app/docs
- **Solo lectura.** No escribe nada.
- **Auth:** header `X-API-Key`. Sirve cualquier rol; no exige rol comercial porque no
  expone precios, márgenes ni bitácora comercial.

> **Cambio de forma (2026-08-18).** Este endpoint devolvía **una fila por planta** en
> `items[]`. Ahora devuelve **un nodo por PPA** en `ppas[]`. Es un reemplazo, no una
> variante: `items` ya no existe.
>
> **Ficha completa de la planta (2026-08-19), aditivo.** `proyectos[].detalles` trae
> ahora la planta como está creada en la plataforma: `identificacion` (el id de la planta
> en cada sistema), `clasificacion`, `tecnica`, `fronteras[]`, `servicios`,
> `construccion` y `simulacion`, más `direccion` y `url_mapa` dentro de `ubicacion`.
> **Ningún campo anterior cambió de nombre, de lugar ni de unidad**: quien ya integra
> sigue leyendo lo mismo y puede ignorar los bloques nuevos.

---

## 1. La idea que hay que entender primero

**Un PPA que no está firmado no existe como contrato.** La oferta del CRM *es* el PPA
hasta que se firma. Eso se lee en un solo campo:

| | `ppa.id` es `null` | `ppa.id` con valor |
|---|---|---|
| Qué es | **Borrador** | Contrato real |
| Fila en `ppa_contratos` | No hay | Sí |
| Aparece en `/servicios` | **No** | Sí |
| De dónde salen las condiciones | De la oferta (tentativas) | Del contrato (pactadas) |

`aparece_en_servicios` viaja como booleano y dice exactamente lo mismo, para no obligar a
deducirlo.

Por eso no hay PPAs de mentira en la base: un borrador no tiene fila, así que no puede
colarse en Cumplimiento, GESCON, liquidaciones ni facturación, que son los módulos que
leen `ppa_contratos`.

---

## 2. `estado`: uno solo

`estado` es la etapa del pipeline comercial, el mismo vocabulario que se ve en el
tablero de `/comercial`: `oferta`, `contrato`, `firmado`, `operando` (y
`terminado` / `declinado` si los pedís por `estado_pipeline`).

**No hay un segundo estado que pueda contradecirlo.** Las tres situaciones que
importan salen de cruzarlo con `id`:

| `estado` | `id` | Qué significa | Qué esperar |
|---|---|---|---|
| `oferta`, `contrato` | `null` | El PPA está **en preparación** | Condiciones tentativas. Es normal que no haya proyectos vinculados |
| cualquiera | poblado | El **contrato existe** | Condiciones del contrato. Aparece en `/servicios` |
| `firmado`, `operando` | `null` | **Inconsistencia.** El negocio cerró y no hay PPA por ningún camino | Es dato por cargar de nuestro lado |

> **Cambio incompatible (2026-08-19).** Antes el nodo traía además
> `etapa_comercial`, `estado_ppa` y `es_borrador`. Ninguno aportaba información
> propia: los tres eran función de la etapa y de si `id` es `null`. Y `estado_ppa`
> reusaba la palabra **firmado** con otro significado —"existe la fila en
> `ppa_contratos`"—, así que un nodo podía traer `estado_ppa: "firmado"` al lado
> de `etapa_comercial: "oferta"` y leerse como una contradicción. Ahora
> `etapa_comercial` se llama **`estado`** y los otros dos se derivan de la tabla
> de arriba. En el sobre, `por_estado_ppa` pasó a **`por_estado`**.

### De dónde sale el contrato: `fuente_ppa`

El PPA se resuelve por **dos caminos**, y el explícito manda:

| `fuente_ppa` | Cómo se encontró |
|---|---|
| `"oferta"` | El enlace que deja `firmar` (`oferta.ppa_contrato_id`) |
| `"proyecto"` | El PPA vigente **de la planta**. Los contratos anteriores al CRM no están enlazados a ninguna oferta, así que este es hoy el camino normal |
| `null` | No hay contrato por ningún camino. Si además `estado` es `firmado` u `operando`, es la inconsistencia de arriba |

Si hay varios contratos en la planta gana el vigente hoy, y entre vigentes el de
compra. Un contrato vencido de 2021 no le gana al que está corriendo.

Esa inconsistencia **no se rellena** inventando un contrato con fechas y tarifas nulas: eso
metería compromisos fantasma en Cumplimiento. Se muestra para que se corrija. Si te
aparece uno, avisá.

---

## 3. Qué entra al árbol

**Solo contratos de energía:** ofertas de `compra_energia` y de `comunidad_energetica`.

- Las ofertas de **servicios** (representación, CGM) **no** salen: desembocan en
  `contratos_servicio`, que es otra entidad. No son PPAs.
- **Comunidad energética no es un tipo aparte**: es un PPA con
  `es_comunidad_energetica: true`.
- Las **salidas** del pipeline (`declinado`, `terminado`) no producen PPA. Un negocio
  caído no es un contrato en preparación. Pedirlas da **422**, no una lista vacía.

Las cuatro etapas que sí producen PPA: `oferta`, `contrato`, `firmado`, `operando`.

---

## 4. La respuesta

```jsonc
{
  "generado_en": "2026-08-18T14:03:11-05:00",
  "estados_pipeline": ["oferta", "contrato", "firmado", "operando"],
  "total": 61,
  "por_estado": { "oferta": 44, "contrato": 5, "firmado": 5, "operando": 7 },
  "ppas": [
    {
      "ppa": {
        "id": null,
        "estado": "oferta",
        "aparece_en_servicios": false,
        "numero_codigo_contrato": null,
        "nombre_interno": null,
        "planta_declarada": "Balmora 1 y 2",
        "condiciones": {
          "origen": "oferta",
          "fecha_inicio": "2026-09-01",
          "fecha_fin": "2033-08-31",
          "duracion_meses": 84,
          "duracion_anios": 7.0,
          "duracion_texto": "7 años",
          "meses_restantes": 84,
          "vigente": false,
          "energia_kwh_mes": 150000.0
        },
        "es_comunidad_energetica": false,
        "cantidad_proyectos": 2,
        "cliente": "INVERSIONES BIOSOSTENIBLES S.A.S.",
        "oferta_id": 33,
        "codigo_seguimiento": "OP.COM No.0051-3-2026",
        "oportunidad_id": 12,
        "tipo_contrato": null,
        "comprador": null,
        "vendedor": null,
        "fuente_ppa": null,
        "ofertas": [
          { "oferta_id": 33, "codigo_seguimiento": "OP.COM No.0051-3-2026",
            "tipo": "compra_energia", "estado": "oferta", "oportunidad_id": 12 }
        ]
      },
      "proyectos": [
        {
          "proyecto_id": 276,
          "nombre": "GD Balmora 1",
          "api_id_unergy": "balmora1",
          "detalles": {
            "estado_proyecto": "en_operacion",
            "estado_proyecto_label": "En operación",
            "potencia_instalada_kwp": 990.0,
            "potencia_con_cen_mw": 0.9,
            "ubicacion": {
              "municipio": "Corozal",
              "departamento": "Sucre",
              "texto": "Corozal, Sucre",
              "latitud": 9.317,
              "longitud": -75.292,
              "direccion": "Vereda Las Peñas, km 3",
              "url_mapa": "https://maps.app.goo.gl/..."
            },
            "operador_red": "ELECTRIFICADORA DEL CARIBE S.A. E.S.P.",
            "operador_red_id": 4,
            "fecha_entrada_operacion": "2025-03-01",
            "fecha_inicio_comercializacion": "2025-04-12",
            "energia_promedio_mensual_mwh": 178.412,
            "energia_promedio_mensual_kwh": 178412.0,
            "energia_promedio_origen": "medido",
            "energia_promedio_detalle": {
              "dias_con_datos": 30,
              "ventana_desde": "2026-07-18",
              "ventana_hasta": "2026-08-17",
              "actualizado_en": "2026-08-17T21:18:45-05:00"
            },
            "identificacion": {
              "nombre_comercial": "GD Balmora 1",
              "nombre_bitacora": "Balmora 1",
              "nombre_clientes": "Balmora",
              "topic_slug": "gd-balmora-1",
              "sub_project": "balmora1",
              "codigo_cnd": "CND-0912",
              "codigo_tsf": "TSF-77",
              "origina_code": "MF-BAL-1",
              "project_id_solenium": "SOL-45",
              "sunfactory_project_id": 310,
              "quoia_nodo_id": 8,
              "quoia_reporte_generacion_id": 91,
              "quoia_reporte_consumo_id": null,
              "portafolio_id": 3,
              "portafolio": "Portafolio Caribe"
            },
            "clasificacion": {
              "clasificacion_regulatoria": "AGGE",
              "tipo_tecnologia": "solar",
              "tipo_proyecto": "minigranja",
              "es_comunidad_energetica": false,
              "nombre_comunidad": null
            },
            "tecnica": {
              "voltaje_red": "13.2 kV",
              "tipo_conexion": "trifásica",
              "potencia_ac_kw": 900.0,
              "capacidad_instalada_kwp": 990.0,
              "produccion_especifica_kwh_kwp": 1450.5,
              "tipo_tracker": "1E",
              "paneles": {
                "cantidad_total": 1800,
                "marca": "Trina",
                "potencia_panel_kwp": "0.55",
                "grupos": [
                  { "marca": "Trina", "modelo": "TSM-550", "potencia_pico_wp": 550.0,
                    "cantidad": 1800 }
                ]
              },
              "inversores": {
                "cantidad": 5,
                "marca": "Huawei",
                "potencia_kwp": "300",
                "cantidad_strings": 48,
                "equipos": [
                  { "id": 812, "nombre": "Inversor 1", "marca": "Huawei",
                    "modelo": "SUN2000-300KTL", "potencia_nominal_kw": 300.0,
                    "tipo": "central", "numero_serie": null }
                ]
              },
              "almacenamiento": {
                "tiene": false, "capacidad_kwh": null, "marca": null, "modelo": null
              },
              "equipos_marcas": {
                "transformador": "Siemens",
                "reconectador_rele": "NOJA Power",
                "totalizador": null,
                "seguidor_solar": null,
                "medidores_frontera": "Elster",
                "modems_frontera": null,
                "cctv": "Hikvision"
              },
              "seguridad_fisica": "Cerco eléctrico",
              "tiene_internet": "si",
              "retie_url": "https://drive.google.com/..."
            },
            "fronteras": [
              {
                "id": 501,
                "codigo_frontera": "Frt00123",
                "nombre_frontera": "FN BALMORA 1 GENERACION",
                "codigo_propio": null,
                "tipo_frontera": "generacion",
                "estado": "activa",
                "nivel_tension_kv": 13.2,
                "capacidad_transporte_mw": 0.9,
                "capacidad_efectiva_mw": 0.9,
                "factor_perdidas": 1.0378,
                "subestacion": "Corozal",
                "punto_conexion": "Circuito COR-12",
                "municipio": "Corozal",
                "departamento": "Sucre",
                "operador_red": "ELECTRIFICADORA DEL CARIBE S.A. E.S.P.",
                "operador_red_id": 4,
                "representante_frontera": "UNERGY S.A.S.",
                "fecha_registro_asic": "2025-02-14",
                "niu": null,
                "es_agrupadora": false
              }
            ],
            "servicios": {
              "operacion": true, "representacion": true, "cgm": true, "ppa": true,
              "promotor": false, "rec": false,
              "fecha_fin_representacion": "2030-12-31"
            },
            "construccion": {
              "fase": "energizado",
              "avance_obra_pct": 100.0,
              "fecha_estimada_energizacion": "2025-02-28",
              "origen_registro": "tsf_sync"
            },
            "simulacion": {
              "p50_mensual_kwh": [172000, 168000, 181000, 175000, 170000, 165000,
                                  178000, 182000, 176000, 174000, 171000, 169000],
              "p90_mensual_kwh": null,
              "p99_mensual_kwh": null,
              "p50_anual_kwh": 2081000.0
            }
          },
          "fuentes": {
            "municipio": "proyecto",
            "departamento": "proyecto",
            "operador_red": "proyecto",
            "estado_proyecto": "proyecto",
            "energia_promedio": "medido",
            "api_id_unergy": "sub_project",
            "fecha_inicio_comercializacion": "proyecto"
          }
        }
      ]
    }
  ]
}
```

### `condiciones`

Periodo, duración y energía del PPA. **`origen` dice si son pactadas o tentativas** —
`"contrato"` o `"oferta"`. Sin ese campo, una fecha de la oferta y una firmada se leen
idénticas.

`energia_kwh_mes` ocupa la misma casilla en los dos casos pero no es el mismo hecho: del
contrato es `cantidad_minima_kwh_mes` (un compromiso), de la oferta es la estimación
técnica de la planta. `origen` avisa.

`meses_restantes` de un contrato que todavía no arrancó es su duración completa, nunca la
distancia hasta el fin. Nunca es mayor que `duracion_meses`.

### `ofertas[]`

**Un PPA es un nodo, aunque lo alimenten varias ofertas.** Si dos ofertas desembocan
en el mismo contrato, sale **un** nodo con las dos en `ofertas[]` — el `total` cuenta
contratos, no ofertas. Lo normal es una sola. Los campos de nivel superior
(`oferta_id`, `codigo_seguimiento`) son los de la oferta principal: manda la de compra
de energía, que es la que define el negocio.

Un borrador no tiene contrato por el que agrupar, así que cada oferta es su propio nodo.

### `proyectos[]` y sus `detalles`

Las plantas del PPA. **De dónde salen depende del estado:** si el contrato existe, son las
de `ppa_contrato_proyectos` (la verdad contractual); si es borrador, las que declaró la
oferta.

Un PPA puede traer **varias** plantas (`cantidad_proyectos`): hay ofertas que cubren dos
("Balmora 1 y 2"). Y puede traer **cero**: hoy el 74% del pipeline no tiene la planta
vinculada como proyecto en la plataforma, y en esos casos `proyectos` viene vacío con
`planta_declarada` como único nombre disponible. El nodo existe igual — el negocio existe.

#### La ficha de la planta

`detalles` es la **ficha de la planta**, y es donde vive lo que el nivel PPA no puede
tener: un contrato de dos plantas no está en un municipio ni tiene un operador de red.

| Campo | Qué es |
|---|---|
| `estado_proyecto` · `estado_proyecto_label` | Estado de la PLANTA (`en_desarrollo`, `en_operacion`, `suspendido`, `cancelado`). **No es la etapa comercial** del PPA: uno dice en qué punto está la planta y el otro en qué punto está el negocio, y pueden discrepar. No se los concilia — conciliarlos taparía el dato mal cargado |
| `potencia_instalada_kwp` | Potencia instalada, en kWp |
| `potencia_con_cen_mw` | Potencia con CEN, en MW. Otro número y otro papel: no se convierte a una sola unidad |
| `ubicacion` | `municipio`, `departamento`, `texto` (los dos ya armados), `latitud`, `longitud`, `direccion` (dirección o vereda) y `url_mapa` (el enlace que cargó operaciones — **no** se fabrica uno con lat/lon: null significa que nadie lo cargó) |
| `operador_red` · `operador_red_id` | Operador de red. El id es del catálogo `operadores_red`, para cruzar |
| `fecha_entrada_operacion` | Cuándo entró en operación la planta |
| `fecha_inicio_comercializacion` | **Primer día con generación real**, autoderivado. No es la fecha del contrato (esa está en `condiciones`) ni la de entrada en operación: tres hechos distintos, tres campos |

#### El resto de la ficha del Proyecto

Todo lo que se diligencia al crear una planta en la plataforma. **Los bloques viajan
siempre**, incluso vacíos: una planta sin ficha técnica cargada es el caso normal, y
devolver el bloque ausente obligaría a programar dos formas para la misma cosa.

Nada de esto tiene escalón de oferta —una oferta comercial no declara marcas de inversor
ni códigos SIC—, así que **sale del Proyecto o sale null**, y por eso no aparece en
`fuentes`.

| Bloque | Qué trae |
|---|---|
| `identificacion` | Los tres nombres de la planta (comercial, bitácora, clientes) y **con qué id se cruza en cada sistema**: `sub_project` (API de generación de Unergy), `project_id_solenium` (data.sole.tech), `sunfactory_project_id` (pipeline de obra), `origina_code`, `codigo_cnd`, `codigo_tsf`, los `codigo_sic_*` de liquidación, los ids de Quoia y el portafolio. Son espacios de ids **distintos** aunque los tres se lean como "el id de la planta": el nombre del sistema está en la clave a propósito |
| `clasificacion` | `clasificacion_regulatoria` (la de la CREG: AGP/AGPE/AGGE/GD/DER — la que manda para el mercado), `tipo_proyecto` (la interna: minigranja/autoconsumo/gd/...), `tipo_tecnologia` y la etiqueta de comunidad energética. Tres ejes independientes: no se derivan uno del otro |
| `tecnica` | Red (`voltaje_red`, `tipo_conexion`, `potencia_ac_kw` — que es AC, distinta de los kWp DC), paneles, inversores, almacenamiento y marcas de equipos. `paneles.grupos` e `inversores.equipos` son las listas **reales cargadas** —los inversores son los que se usan para reportar fallas por inversor— y `cantidad`/`marca` son el resumen que declaró el diseño: pueden no coincidir, y por eso viajan los dos. Los `equipos` excluyen los dados de baja. **No** salen IPs de módem ni contraseñas de medidor, que están en la misma tabla: esta superficie es de consulta |
| `fronteras[]` | Las fronteras comerciales: **con qué código se liquida**. No pueden vivir a nivel de PPA (una planta tiene generación y consumo; un contrato de dos plantas tiene las de las dos). Traen código, tipo, estado, nivel de tensión, capacidad, subestación, punto de conexión y el operador de la frontera. Las borradas no salen; las credenciales de medidor tampoco |
| `servicios` | Qué le presta Unergy a esta planta (`operacion`, `representacion`, `cgm`, `ppa`, `promotor`, `rec`) más `fecha_fin_representacion`. Son los **flags** del Proyecto —qué está activo—, no los contratos que los respaldan, que son otra entidad |
| `construccion` | `fase` (en_construccion / pruebas / proximo_energizar / energizado), `avance_obra_pct` y `fecha_estimada_energizacion`, de Sun Factory. Complementa `estado_proyecto`, que se queda en `en_desarrollo` toda la obra y no separa una planta en cimientos de una que se energiza la semana entrante |
| `simulacion` | La curva simulada, 12 valores en kWh con **índice 0 = enero**. Los tres escenarios viajan porque no son intercambiables (el P50 es el esperado; con P90/P99 se estructura el negocio). Es una **proyección**, distinta de `energia_promedio_mensual_*`, que puede ser medida. `p50_anual_kwh` solo se calcula con la serie completa: sumar 7 meses y llamarlo anual sería mentira |

#### `fuentes`

De dónde salió cada campo de la ficha. Sin ese mapa "no aplica" y "todavía no lo
sabemos" se ven idénticos, y un operador del catálogo se lee igual que uno de texto libre.

| Valor | Qué significa |
|---|---|
| `proyecto` | Del Proyecto en la plataforma. Es el bueno |
| `frontera` | El operador salió de la frontera de la planta, porque el Proyecto no tiene vínculo propio. `operador_red_id` es el de la frontera |
| `proyecto_legacy` | `proyectos.operador_red`, texto libre sin validar contra el catálogo. Viaja **sin** `operador_red_id`: no se puede cruzar |
| `oferta` | Lo declaró la oferta comercial porque la planta no lo tiene cargado |
| `null` | Nadie lo aportó. **No es un error** |

**Lo que declara la oferta es una afirmación sobre la planta que la oferta nombró.** Si un
PPA cubre dos plantas y la oferta nombró una, la hermana no hereda su municipio, su
operador ni su energía declarada: sale en null, que es la verdad — nadie declaró nada
sobre ella.

`energia_promedio_origen` **hay que leerlo siempre**:

| Valor | Qué es | Confiabilidad |
|---|---|---|
| `medido` | Promedio de generación real de los últimos 30 días | Alta |
| `manual` | Lo cargó una persona (planta sin histórico) | Media |
| `estimado` | Proyección de ingeniería (curva P50) | Baja para operación |
| `declarado` | Lo declaró la oferta; la planta no existe en la plataforma | Baja |
| `null` | Nadie lo aportó. **No es un error** | — |

**No hay energía acumulada en la respuesta, a propósito.** `api_id_unergy` es la llave para
consultarla contra la API de generación de Unergy cuando se necesite: calcular acumulados
históricos por planta en cada request haría lenta una llamada que se usa en cada refresco.

---

## 5. Filtros

| Parámetro | Qué hace |
|---|---|
| `estado_pipeline` | Acota a etapas: `oferta` · `contrato` · `firmado` · `operando`. Repetible. Por defecto vienen las cuatro |
| `todas_las_etapas` | Ya es el comportamiento por defecto. Se conserva para no romper llamadas que lo vienen mandando |
| `q` | Busca en planta declarada, cliente, código de seguimiento, código de contrato y nombre de cada proyecto |

Acotar la etapa **elige PPAs, no recorta su contenido**: un PPA que entra sigue trayendo
todas sus plantas.

```bash
export BASE="https://backend-production-63d8.up.railway.app/api/v1"

# todo el pipeline de contratos
curl "$BASE/comercial/proyectos-operando" -H "X-API-Key: $UNERGY_API_KEY"

# solo los contratos ya firmados y operando
curl "$BASE/comercial/proyectos-operando?estado_pipeline=firmado&estado_pipeline=operando" \
  -H "X-API-Key: $UNERGY_API_KEY"

# solo los borradores
curl "$BASE/comercial/proyectos-operando?estado_pipeline=oferta&estado_pipeline=contrato" \
  -H "X-API-Key: $UNERGY_API_KEY"
```

---

## 6. Errores

| Código | Cuándo |
|---|---|
| 401 | Falta el header `X-API-Key`, o la key no existe / está desactivada |
| 422 | `estado_pipeline` recibió algo que no es una de las cuatro etapas con PPA (incluye `declinado` y `terminado`, que existen en el CRM pero nunca tienen contrato) |

Un resultado vacío no es un error: 200 con `total: 0`, `ppas` vacío y `por_estado` vacío.

---

## 7. Recomendaciones

- **Cacheá de tu lado.** Los datos cambian a lo sumo una vez al día. Una vez por hora sobra.
- **Timeout de 60 s**: la llamada tarda menos de un segundo, pero Railway puede tener
  arranque en frío.
- **`generado_en`** dice de cuándo es la foto. Mostrala si tu tablero cachea.

---

## 8. Cómo se materializa un PPA

`POST /comercial/ofertas/{id}/firmar` es la única puerta: crea la fila en `ppa_contratos`
con las condiciones pactadas, expande la tabla de precios anuales a tarifas mensuales,
pasa **todas** las plantas de la oferta al contrato y mueve la oferta a `firmado`. Desde
ese momento el PPA aparece en `/servicios` y su nodo deja de ser borrador.

Es idempotente por enlace: si la oferta ya tiene contrato responde 409 en vez de crear un
segundo.

---

## 9. Contacto

Juan José (juanjose@unergy.io) para API keys, para reportar datos en `null` o
`sin_contrato`, y para pedir campos nuevos.
