# Entrega — API de Proyectos Firmados y en Operación

Paso a paso para que Juan José le entregue la API a su compañera y ella la integre en la
otra plataforma.

**Qué se entrega:** un endpoint que devuelve, en una sola llamada, todas las plantas con
negocio cerrado en `/comercial` — etapas **Firmado** y **Operando** — con nombre, **estado
comercial**, **estado del proyecto**, ubicación, operador de red, generación mensual promedio, fecha de inicio de comercialización,
tiempo del contrato de energía y **API ID de Unergy**.

Cada fila trae **dos estados**. El comercial, `estado_pipeline`:

- `firmado` — hay contrato, el suministro todavía no arrancó. Es normal que no tenga
  generación promedio ni fecha de comercialización: la planta aún no entrega energía.
- `operando` — ya está entregando.

Una planta con la energía operando y los servicios recién firmados sale como `operando` (la
etapa más avanzada de sus ofertas). Con `?estado_pipeline=operando` o `=firmado` se acota, y
con **`?todas_las_etapas=true`** viene el pipeline completo: `oportunidad`, `oferta`,
`contrato`, `firmado`, `operando`, `terminado` y `declinado`.

Y el del proyecto, `estado_proyecto` (`en_desarrollo` · `en_operacion` · `suspendido` ·
`cancelado`), con su etiqueta lista en `estado_proyecto_label` ("En operación"). Es `null` en
las plantas que todavía no están vinculadas a un proyecto de la plataforma. Los dos estados
pueden discrepar y la API **no los concilia**: los muestra como están.

```
GET https://operaciones.unergy.io/api/v1/comercial/proyectos-operando
Header: X-API-Key: uop_…
```

La guía técnica completa para ella es **`Backend Operaciones/docs/API_PROYECTOS_OPERANDO.md`**.
Este documento es solo el trámite de entrega.

---

## Paso 1 — Crear el usuario de la integración (5 min)

**No le des tu API key ni una key de tu usuario admin.** Las keys heredan **todos** los
permisos del usuario dueño, y hoy los `scopes` de la key no se aplican: una key marcada
"read" puede escribir. El único límite real es el **rol del usuario** al que cuelga.

1. Entrá a https://operaciones.unergy.io/admin/usuarios
2. **Nuevo usuario**
   - Nombre: `Integración <plataforma de tu compañera>`
   - Email: algo identificable, p. ej. `integracion-<plataforma>@unergy.io`
   - Rol: **`monitoreo`** (el más acotado que sirve). Si esa opción no aparece, usá
     `operaciones`. **No uses `admin`.**
3. Guardar.

> Un usuario propio por integración: si algo sale mal, se desactiva esa cuenta y no se
> rompe nada más. Si le das una key de tu usuario, apagarla te deja a vos sin acceso.

## Paso 2 — Generar la API key (1 min)

1. En la misma pantalla, en la fila del usuario nuevo, tocá el ícono de **llave** 🔑
2. Nombre de la key: `Integración <plataforma>`
3. **Generar**
4. **Copiá la key ahí mismo** — se muestra una sola vez. Empieza con `uop_` y tiene 68
   caracteres.

## Paso 3 — Probar la key antes de mandarla (1 min)

Pegá esto en una terminal, reemplazando la key:

```bash
curl "https://operaciones.unergy.io/api/v1/comercial/proyectos-operando" \
  -H "X-API-Key: uop_TU_KEY_ACA"
```

Tiene que responder un JSON que arranca con `{"generado_en": …, "estados_pipeline":
["firmado", "operando"], "total": N, …}`.

- Si da **401** → la key quedó mal copiada (tienen que ser 68 caracteres).
- Si da **200 con `total: 0`** → no hay ofertas en etapa Operando en el CRM; ver la sección
  "Antes de entregar" más abajo.

## Paso 4 — Mandarle esto a tu compañera

> **La API key va por un canal privado** (WhatsApp directo, gestor de contraseñas), nunca en
> un correo grupal, un ticket, ni un repo.

Texto para copiar y pegar:

---

Hola, te paso la API con nuestras plantas firmadas y operando.

**Endpoint (una sola llamada, trae todo):**
```
GET https://operaciones.unergy.io/api/v1/comercial/proyectos-operando
Header: X-API-Key: <te la mando aparte>
```

Devuelve una entrada por planta con: nombre, **estado comercial**, **estado del proyecto**,
ubicación, operador de red,
generación mensual promedio, fecha de inicio de comercialización, duración del contrato de
energía y el **API ID de Unergy**.

Prueba rápida:
```bash
curl "https://operaciones.unergy.io/api/v1/comercial/proyectos-operando" \
  -H "X-API-Key: LA_KEY"
```

Cinco cosas para tener en cuenta al integrar:

1. **Hay dos estados por fila y responden preguntas distintas.**

   `estado_pipeline` — en qué punto está el **negocio**:
   - `firmado` → hay contrato pero el suministro no arrancó. Es **normal** que no tenga
     generación promedio ni fecha de comercialización: la planta todavía no entrega energía.
   - `operando` → ya está entregando.

   El sobre trae `por_estado_pipeline` con el conteo de cada una, y podés acotar con
   `?estado_pipeline=operando` o `=firmado`. Ojo: si una planta tiene varias ofertas,
   `estado_pipeline` es la etapa más avanzada de todas.

   `estado_proyecto` — en qué punto está la **planta**: `en_desarrollo`, `en_operacion`,
   `suspendido` o `cancelado`, con la etiqueta lista para mostrar en `estado_proyecto_label`
   ("En operación"). Viene en `null` si esa oferta todavía no está vinculada a una planta
   cargada de nuestro lado.

   Los dos pueden **no coincidir** (oferta operando sobre un proyecto `en_desarrollo`): son
   dos hechos distintos y no los conciliamos. Si te topás con una discrepancia que te
   estorba, avisame: es dato por corregir nuestro.

   Sin filtro vienen solo las cerradas (firmado + operando). Con `?todas_las_etapas=true`
   viene todo el pipeline, incluidas prospección, negociación, terminadas y declinadas.
2. **`oferta_vigente` te ahorra recorrer la lista.** Una planta puede tener dos ofertas vivas
   al tiempo (la de compra de energía y la de servicios/CGM), así que `ofertas[]` es una
   lista. Pero el caso normal es una sola, y esa viene suelta en `oferta_vigente` (si dos
   están en la misma etapa, manda la de compra de energía). Es `null` solo cuando la planta
   no tiene nada vivo, o sea que todas sus ofertas están terminadas o declinadas.
3. **Los campos pueden venir en `null`** cuando ese dato todavía no está cargado de nuestro
   lado. Cada respuesta trae un objeto `fuentes` que dice de dónde salió cada valor, para
   que puedas distinguir "no aplica" de "todavía no lo sabemos". Si ves muchos `null`,
   avisame — es dato faltante nuestro, no un error de la API.
4. **`gen_promedio_origen`** dice si la generación promedio es `medido` (real, últimos 30
   días), `manual`, `estimado` o `declarado`. Mostralo al lado del número: no todos valen
   lo mismo.
5. **Cachéalo de tu lado.** Los datos cambian a lo sumo una vez al día; con consultar una
   vez por hora sobra.

La documentación completa (esquema de todos los campos, ejemplos en Python y JS, errores)
está en el archivo `API_PROYECTOS_OPERANDO.md` que te adjunto.

Cualquier cosa me escribís.

---

*(Adjuntale `Backend Operaciones/docs/API_PROYECTOS_OPERANDO.md`.)*

---

## ✅ Estado de los datos — YA EJECUTADO el 2026-08-10

Los backfills y el vinculado **ya se corrieron**. Ahora la API devuelve **36 plantas: 31
operando + 5 firmadas.**

| Campo | Antes | Ahora |
|---|---|---|
| Plantas devueltas | 32 (solo operando) | **36** (31 operando + 5 firmadas) |
| …con nombre | 32 | **36/36** |
| …vinculadas a un proyecto | 4 | **28/36** |
| …con API ID de Unergy | — | **28/36** |
| …con operador de red | 4 | **27/36** |
| …con ubicación | 1 | **8/36** ⚠️ |
| …con contrato de energía | 4 | **17/36** (ver nota abajo) |

Sobre las **31 que ya operan** (en las firmadas estos dos campos no aplican todavía, porque
la planta no entrega energía):

| Campo | Antes | Ahora |
|---|---|---|
| con fecha de inicio de comercialización | 4 | **22/31** |
| con generación promedio | 1 | **17/31** (todas `medido`) |

> **Las 5 firmadas no tienen `contrato_energia`, y está bien.** Sus ofertas son todas de
> **servicios** (`OP.REPCGM…`: representación / CGM), no de compra de energía, así que no hay
> PPA que reportar — el contrato de representación vive en otra tabla. `ofertas[].tipo` lo
> dice: `servicios_operacionales`. Si alguna llegara a tener una oferta `compra_energia` sin
> PPA, ahí sí faltaría cargarlo.

Qué se ejecutó:

0. `POST /comercial/ofertas/vincular-proyectos?dry_run=false` sobre las firmadas →
   **3 vínculos más** (Sirius → GD Sirius, AGUSTÍN 2 → GD Agustín 2, AGUSTÍN 3 → GD Agustín 3),
   todos con puntaje 1.00
1. `POST /comercial/ofertas/vincular-proyectos?dry_run=false` → **21 vínculos aplicados**
2. `POST /cumplimiento/backfill-comercializacion?dry_run=false` → 0 nuevos (112 proyectos no
   tienen identificador de monitoreo), pero al vincular quedaron expuestas las fechas que ya
   estaban cargadas: pasó de 4 a 22
3. `POST /proyectos/gen-promedio/recalcular?dry_run=false` → **48 proyectos actualizados**,
   0 fallidos
4. `POST /proyectos/backfill-ubicacion?dry_run=false` → 0 de 149: **Sun Factory y Solenium no
   tienen la ubicación**, hay que cargarla a mano

### Lo que queda pendiente

**Ubicación (5/31)** — es el hueco real. Los proyectos no tienen `municipio`/`departamento`
cargados y las fuentes externas tampoco. Se carga a mano desde el detalle de cada proyecto.
La API no lo disimula: `fuentes.municipio: null` significa "nadie lo cargó todavía", así que
tu compañera puede distinguirlo de un dato real.

**2 vínculos que dejé sin aplicar a propósito** (decisión tuya, no mía):

| Oferta | Propuesta | Por qué la dejé afuera |
|---|---|---|
| 24 | GD La Hormiga → **GD La Hormiguita** | No existe ningún proyecto "GD La Hormiga". El diminutivo puede ser OTRA planta |
| 33 | GD ISABELA 1 y GD ISABELA 2. → **GD Isabela** | Esa oferta nombra DOS plantas; apuntarla a un solo proyecto mostraría la generación de una como si fuera de ambas |

Para aplicar cualquiera de las dos, cuando decidas:

```bash
curl -X POST "$BASE/comercial/ofertas/vincular-proyectos?dry_run=false&oferta_id=24" \
  -H "X-API-Key: $KEY"
```

**"Agustín" (oferta 15, firmada) quedó sin vincular por ambigüedad:** existen GD Agustin 1,
GD Agustín 2 y GD Agustín 3, y el nombre suelto no dice cuál. Decidí vos y aplicá con
`oferta_id=15`.

**5 plantas con una inconsistencia que vale la pena revisar:** el CRM dice que la oferta está
*operando*, pero el estado del proyecto dice que no está en operación — por eso quedan sin
generación promedio (el cálculo solo mira proyectos `en_operacion`). **Desde que la API
devuelve `estado_proyecto`, esto se ve desde afuera:** son las filas con
`estado_pipeline: "operando"` y `estado_proyecto` distinto de `en_operacion`.

- AGGE Extractora Monterrey (267) · GD Isabela (276) · GD Taurus IX (262) ·
  GD Taurus X (263) · MGS Naos 2 (33, marcado *cancelado*)

O la etapa del CRM está mal, o el estado del proyecto está mal. Cualquiera de las dos que
corrijas, la generación aparece sola en el siguiente recálculo.

**2 plantas sin generación por falta de lecturas** (Bayunca, GD San Pelayo): la ventana de 30
días no tuvo suficientes días con dato. Es cobertura de monitoreo, no un problema de la API.

---

## Referencia — cómo se corren esos pasos (para la próxima vez)

### A. Vincular las ofertas del CRM con las plantas (el paso importante)

**28 de las 32 ofertas no apuntan a ningún proyecto.** No es que las plantas no existan: el
CRM se cargó desde hojas donde la planta es texto libre, así que quedaron escritas distinto.

| En el CRM dice | La planta se llama |
|---|---|
| Catedral | La Catedral |
| Taurus IX | GD Taurus IX |
| Parque Solar Baraya | Minigranja Solar Baraya |
| Marimondá | GD Marimonda |
| San Pelayo | GD San Pelayo |

Como los datos (ubicación, operador, generación, contrato) viven en el **proyecto**, sin ese
vínculo la API no tiene de dónde sacarlos.

```bash
export KEY="uop_TU_KEY_ADMIN"
export BASE="https://operaciones.unergy.io/api/v1"

# 1) ver qué propone — NO escribe nada
curl -s -X POST "$BASE/comercial/ofertas/vincular-proyectos?dry_run=true" -H "X-API-Key: $KEY"
```

La respuesta trae tres listas:

- **`propuestos`** — lo que vincularía, con el nombre de la planta, el del proyecto y el
  puntaje. **Revisá esta lista planta por planta.**
- **`sin_candidato`** — no encontró nada suficientemente parecido, con el mejor puntaje que
  vio: si está cerca de 0.72 hay que mirarlo a mano; si está bajo, probablemente la planta
  no existe todavía en la plataforma.
- **`sin_nombre`** — ofertas sin nombre de planta.

#### Lo que dio el dry-run el 2026-08-09 (21 de 28)

Ya lo corrí en seco. Esto es lo que propondría — **revisalo antes de aplicar**:

| Puntaje | En el CRM | Se vincularía a |
|---|---|---|
| 1.00 | Delta 1 · Delta 2 | GD Delta 1 · GD Delta 2 |
| 1.00 | Biosolar | GD Biosolar |
| 1.00 | Astrolumen La Garita | GD Astrolumen La Garita |
| 1.00 | Marimondá | GD Marimonda |
| 1.00 | Yuan Solar | GD Yuan Solar |
| 1.00 | Polaris 2 | GD Polaris 2 |
| 1.00 | Taurus IX · Taurus X | GD Taurus IX · GD Taurus X |
| 1.00 | San Pelayo | GD San Pelayo |
| 1.00 | Catedral *(dos ofertas)* | La Catedral |
| 1.00 | GRANJA 9 CIENAGA | Sol&Cielo 9 - Cienaga |
| 1.00 | GRANJA 7 BONGOS | Sol Y Cielo 7 Los Bongos |
| 0.90 | EXTRACTORA MONTERREY S.A.S. | AGGE Extractora Monterrey |
| 0.88 | Bayunca I | Bayunca |
| 0.85 | Yurbaqua | PSF - Yurbaqua |
| 0.85 | La Paz Leyenda | MGS 0018 La Paz Leyenda |
| 0.85 | Parque Solar Baraya | Minigranja Solar Baraya |
| 0.85 | El Merengue | MGS 0019 El Merengue |
| **0.82** | **GD La Hormiga** | **GD La Hormiguita** ⚠️ |

⚠️ **La única que yo miraría dos veces es "GD La Hormiga" → "GD La Hormiguita".** No hay
ningún proyecto llamado "GD La Hormiga", así que o es la misma planta escrita en diminutivo,
o es una planta distinta que todavía no está cargada. Vos sabés cuál de las dos. Si no estás
seguro, aplicá las otras 20 con `oferta_id` y dejá esa afuera.

Las otras 20 son la misma planta escrita distinto; las de puntaje 1.00 son idénticas una vez
que se quitan los prefijos de ruido ("GD", "Minigranja", "Solar").

#### Las 7 que quedaron sin vincular, y por qué

| Planta en el CRM | Qué pasa |
|---|---|
| San Onofre | **Ambiguo**: existen "Minigranja 0061 - San Onofre" y "Minigranja 0062 - San Onofre 2". No adivina a propósito — decidí vos y aplicá con `oferta_id` |
| GD ISABELA · GD ISABELA 1 y GD ISABELA 2. | **No existe ningún proyecto "Isabela"** en la plataforma. Hay que crearlo |
| GD Las Margaritas 1 - | La planta todavía no existe como proyecto |
| GD Rio Pamplonita | La planta todavía no existe como proyecto |
| Inversiones tecni-plast S.A.S. | En la casilla de la planta quedó el nombre de la **empresa**. Hay que corregir la oferta |
| SOLUCIONES … SONETEL S.A.S | Ídem: nombre de empresa, no de planta |

Cuando la lista te convenza:

```bash
# 2) aplicarlo
curl -s -X POST "$BASE/comercial/ofertas/vincular-proyectos?dry_run=false" -H "X-API-Key: $KEY"

# ¿solo algunas? pasá los ids que aceptás (los que no vayan, no se tocan)
curl -s -X POST "$BASE/comercial/ofertas/vincular-proyectos?dry_run=false&oferta_id=12&oferta_id=45" \
  -H "X-API-Key: $KEY"
```

Es idempotente (solo toca ofertas sin proyecto) y se deshace poniendo el proyecto en NULL
desde la ficha de la oferta en `/comercial`. **Sirve para el CRM también**, no solo para esta
API: con el vínculo puesto, la oferta muestra los datos de su planta en toda la plataforma.

> Lo dejé sin correr a propósito: escribe en el CRM y quería que vieras la lista antes.

### B. ¿Hay plantas en etapa "Operando" que falten?

Abrí https://operaciones.unergy.io/comercial y filtrá por la etapa
**Operando**. Lo que veas ahí es exactamente lo que devuelve la API. Si falta alguna planta
que sí está operando, hay que moverla de etapa en el CRM.

### C. ¿Están cargados los dos datos que suelen faltar?

Con tu key de admin:

```bash
export KEY="uop_TU_KEY_ADMIN"
export BASE="https://operaciones.unergy.io/api/v1"

# 1) generación promedio: cuáles no la tienen
curl -s "$BASE/proyectos/gen-promedio?solo_faltantes=true" -H "X-API-Key: $KEY"

# 2) fecha de inicio de comercialización: cuáles no la tienen
curl -s "$BASE/cumplimiento/sin-fecha-comercializacion" -H "X-API-Key: $KEY"
```

Para llenarlos:

```bash
# generación promedio — primero en seco, mirá el reporte, después de verdad
curl -X POST "$BASE/proyectos/gen-promedio/recalcular?dry_run=true"  -H "X-API-Key: $KEY"
curl -X POST "$BASE/proyectos/gen-promedio/recalcular?dry_run=false" -H "X-API-Key: $KEY"

# fecha de inicio de comercialización — mismo patrón
curl -X POST "$BASE/cumplimiento/backfill-comercializacion?dry_run=true"  -H "X-API-Key: $KEY"
curl -X POST "$BASE/cumplimiento/backfill-comercializacion?dry_run=false" -H "X-API-Key: $KEY"
```

Las dos tareas son **idempotentes** y **no pisan lo cargado a mano**. El recálculo de
generación tarda unos minutos porque consulta la API de generación planta por planta.

Las plantas que queden sin promedio (sin histórico, recién energizadas) se cargan a mano
desde el detalle del proyecto; la API las marca con `gen_promedio_origen: "manual"`.

> **Orden importante:** primero A (vincular), después C (backfills). Los backfills llenan
> datos del *proyecto*, y hasta que la oferta no esté vinculada a su proyecto, la API no los
> ve.

### Comprobación final

```bash
curl -s "$BASE/comercial/proyectos-operando" -H "X-API-Key: $KEY" \
| python -c "
import sys, json
d = json.load(sys.stdin); n = d['total']
c = lambda f: sum(1 for i in d['items'] if f(i))
print(f'{n} plantas operando')
print(f'  con proyecto vinculado : {c(lambda i: i[\"proyecto_id\"])}/{n}')
print(f'  con ubicacion          : {c(lambda i: i[\"ubicacion\"][\"texto\"])}/{n}')
print(f'  con operador de red    : {c(lambda i: i[\"operador_red\"])}/{n}')
print(f'  con gen promedio       : {c(lambda i: i[\"gen_promedio_mensual_mwh\"] is not None)}/{n}')
print(f'  con inicio comercial.  : {c(lambda i: i[\"fecha_inicio_comercializacion\"])}/{n}')
print(f'  con contrato           : {c(lambda i: i[\"contrato_energia\"][\"duracion_meses\"] is not None)}/{n}')
"
```

Cuando esos números te gusten, entregá.

---

## Si algo falla después

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| Ella recibe 401 de un día para otro | La key se desactivó o el usuario quedó inactivo | Admin → Usuarios → 🔑 → ver si está activa |
| `total: 0` de golpe | Nadie tiene etapa Operando en `/comercial` | Revisar el CRM |
| Una planta dejó de aparecer | El job diario la pasó a `terminado` (se venció el contrato) | Es el comportamiento esperado; si el contrato se renovó, actualizar la `fecha_fin` del PPA |
| Todo en `null` para una planta | La planta no existe como Proyecto, solo como oferta | Vincular el proyecto a la oferta en `/comercial` |

**Para cortar el acceso:** Admin → Usuarios → 🔑 → pausar o eliminar la key. Efecto
inmediato, no hace falta desplegar nada.

---

## Nota técnica (para vos, no para ella)

- El endpoint **no exige rol comercial**, a diferencia del resto de `/comercial`: es de solo
  lectura y no expone precios, márgenes ni bitácora. Con cualquier cuenta activa responde.
  Si querés endurecerlo, es una línea en `app/api/v1/comercial.py`
  (`list_proyectos_operando`).
- El endpoint es **aditivo**: no toca nada de lo que ya existía. `/comercial/ofertas` y la
  vista del CRM siguen igual.
- Sigue pendiente, de antes: **los `scopes` de las API keys no se validan**. Por eso el
  Paso 1 insiste en un usuario propio con rol acotado — hoy el rol es el único límite real.
