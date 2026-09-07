# Separación de dominios — paso previo a la migración a Django

Estado del código al 2026-09-03: 115 tablas en 39 archivos de modelo, 48 routers
(`app/api/v1/`, 28 953 líneas), 25 647 líneas de servicios y un `app/main.py` de
2 665 líneas que es en realidad el planificador y los seeds.

Este documento define **a qué app de Django va cada tabla, cada router y cada
servicio**. No es un refactor: es el mapa que hace posible portar por rebanadas
verticales en vez de todo de una vez.

---

## 0. Lo primero: no migrar dos veces

`app/domains/` ya existe con 15 subdirectorios (`clientes/`, `comercial/`,
`contabilidad/`…), cada uno con `models/ schemas/ services/`. **Están todos
vacíos: 0 archivos `.py`, ni siquiera están en git.** Alguien ya empezó esta
separación por el `mkdir` y se detuvo ahí.

No la retomes. Reorganizar el árbol de FastAPI para después portarlo a Django es
hacer el trabajo dos veces: el resultado intermedio no se despliega, no se testea
distinto y se descarta completo el día del port. La separación de dominios se
consume como **documento** — este — y se materializa **directamente como apps de
Django**, una por rebanada.

`app/domains/` y `app/shared/` (también vacío) deberían borrarse: son ruido que
sugiere una estructura que no existe.

---

## 1. La forma real del acoplamiento

El grafo de claves foráneas entre archivos de modelo da una respuesta limpia:
hay un **núcleo** al que apunta casi todo, y el resto son hojas.

```
                    ┌──────────────────────────────────┐
   32 de 39         │  usuarios   proyectos   clientes │
   archivos de      │  contratos  fronteras            │   ← NÚCLEO
   modelo apuntan   │  operadores_red                  │
   acá adentro      └──────────────────────────────────┘
                              ▲  ▲  ▲  ▲
        ┌─────────────────────┘  │  │  └─────────────────────┐
     monitoreo               energía  mercado_xm         contabilidad
     om · arriendos          garantías · retos · mandatos · registros_cnd
```

Medido por importaciones, no por intuición:

| Módulo | Lo importan | Es |
|---|---|---|
| `models/proyectos` | **52 módulos** | el hub absoluto |
| `models/fronteras` | 23 | núcleo |
| `models/contratos` | 19 | núcleo |
| `models/usuarios` | 14 | núcleo (auth) |
| `models/clientes` | 14 | núcleo |
| `models/retos` | 1 | hoja pura |

**Consecuencia para el orden de migración:** el núcleo se porta primero y
completo. Cualquier rebanada que se intente antes arrastra medio esquema.

---

## 2. Las apps propuestas

19 apps de Django, ya creadas bajo `apps/` con sus 118 tablas (ver
`apps/README.md`). Promedio de 6 tablas por app — no es excesivo, es lo que ya
hay. Marcadas ⚠ las que exigen partir un archivo actual.

### Núcleo (se portan primero, en este orden)

| App | Tablas | Routers actuales | Notas |
|---|---|---|---|
| `plataforma` | `usuarios`, `notificaciones`, `informes_guardados` | `auth`, `api_keys`, `notificaciones`, `informes` | Auth, API keys, auditoría (`services/audit.py`) |
| `proyectos` | `proyectos`, `portafolios`, `proyecto_info_tecnica`, `proyecto_inversores`, `proyecto_inversionistas`, `proyectos_pendientes_ignorados`, `gestion_registros`, `costos_variables`, `verificacion_costos`, `promotor_catalogo_requisitos`, `promotor_seguimientos` | `proyectos`, `portafolios`, `mapa`, `proximos_energizar`, `verificacion_costos`, `dashboard` | El hub. Nada avanza sin esto |
| `clientes` | `clientes`, `cliente_documentos_comerciales`, `cliente_tasa_servicio`, `contactos`, `proyecto_area_contacto` | `clientes` | |
| `fronteras` | `fronteras`, `fronteras_quoia_ignoradas`, `contrato_frontera`, `operadores_red`, `operadores_red_contactos` | `fronteras`, `operadores_red` | |

### Negocio

| App | Tablas | Routers actuales |
|---|---|---|
| `comercial` | `oportunidades`, `oportunidad_estado_historial`, `oportunidad_gestiones`, `oportunidad_ofertas` | `comercial` |
| ⚠ `contratos` | `contratos_servicio`, `pagos_servicio`, `polizas` | `contratos_servicio`, `polizas` |
| ⚠ `ppa` | `ppa_contratos`, `ppa_responsables`, `ppa_tarifas`, `ppa_compromisos_energia`, `ipp_mensual` | `ppa` |
| ⚠ `facturacion` | `factura_agrupacion`, `factura_orden`, `factura_emitida`, `contrato_factura` | `facturacion` |
| ⚠ `mercado_xm` | `despacho_contrato_dia`, `despacho_contrato_mensual`, `precio_bolsa_mensual`, `asic_solicitudes`, `asic_cambios_contratos`, `gescon_diccionario_contratos`, `cumplimiento_mensual`, `clasificacion_energia_mensual`, `rec_procesos` | `asic`, `cumplimiento`, `clasificacion_energia` |
| `liquidaciones` | `liquidaciones`, `liquidacion_costos`, `liquidacion_facturas` | `liquidaciones`, `liquidaciones_proxy` |
| `registros_cnd` | `registro_conexion`, `registro_etapa`, `registro_transicion`, `registro_hito`, `registro_parametros_93`, `registro_equipo_frontera`, `registro_documento`, `registro_alerta` | `registros_cnd` |
| `energia` | `generacion_diaria`, `reporte_energia_generacion`, `reporte_energia_exclusiones`, `reporte_energia_consumo` | `generacion`, `solar`, `generacion_solar`, `reporte_energia`, `reporte_cgm` |
| `monitoreo` | `fallas` y sus 5 catálogos, `fallas_seguimientos`, `fallas_intervalos`, `falla_inversores`, `alertas`, `mantenimientos`, `starlink_facturas`, `starlink_mapeo_sitio`, `starlink_factura_linea` | `fallas`, `monitoreo`, `alertas`, `reconectadores`, `starlink` |
| `om` | `om_ipc_tasas`, `om_seleccion_mensual`, `om_factura_mensual`, `om_pagina_sin_match`, `om_documento_proyecto`, `proyecto_informe_om` | `om`, `informe_om` |
| `arriendos` | `arr_proyectos`, `arr_arrendador`, `arr_ipc_tasas`, `arr_documento`, `arr_seleccion_mensual` | `arriendos` |
| `contabilidad` | `panel_contable`, `panel_contable_linea`, `clasificacion_liquidacion`, `mapeo_celda_concepto`, `alias_fuente_ingreso`, `panel_soporte`, `panel_consecutivo` | `panel_contable`, `estados_resultados` |
| `mandatos` | `mandatos`, `mandato_inversionistas`, `mandato_correos`, `finanzas_mandatos`, `liquidacion_mandatos`, `liquidacion_mandato_lineas` | `mandatos`, `finanzas_mandatos` |
| `garantias` | `gar_calculo`, `gar_componente_real`, `gar_componente_pred`, `xm_archivo`, `xm_medida`, `garantias_ajustes`, `garantia_snapshot`, `garantia_pagado`, `balcttos_neto` | `garantias_modelo`, `garantias_ajustes`, `garantias_proyecciones` |
| `retos` | `retos_trimestre`, `retos_metrica`, `retos_valor_semanal` | `retos` |

**Fuera de toda app:** `evo_proxy` y `liquidaciones_proxy` no tienen modelo — son
pasarelas HTTP. En Django van como `integraciones/` o directamente como un
servicio de dominio con su cliente, no como app.

---

## 3. Las tres fallas geológicas

Tres archivos actuales cruzan fronteras de dominio. Partirlos es el trabajo real
de esta etapa; todo lo demás es mover archivos.

### `app/models/contratos.py` — 476 líneas, 14 tablas, 4 dominios

Es el peor caso del repo. Contiene, mezclados:

| Tablas | Dominio real |
|---|---|
| `contratos_servicio`, `pagos_servicio` | `contratos` |
| `ppa_contratos`, `ppa_responsables`, `ppa_tarifas`, `ppa_compromisos_energia`, `ipp_mensual` | `ppa` |
| `factura_agrupacion`, `factura_orden`, `factura_emitida`, `contrato_factura` | `facturacion` |
| `despacho_contrato_dia`, `despacho_contrato_mensual`, `precio_bolsa_mensual` | `mercado_xm` |

19 módulos lo importan, y cada uno cree que importa "contratos". El corte va acá
y va primero: mientras exista este archivo, `ppa`, `facturación` y `mercado_xm`
no son dominios separables.

### `app/api/v1/cumplimiento.py` — 3 805 líneas

El archivo más grande del proyecto, y un router no debería tener 3 800 líneas de
nada. Importa `asic`, `cumplimiento`, `contratos`, `liquidaciones` y `proyectos`
(también `mantenimiento_impacto`, eliminado el 2026-09-07). Antes de portarlo hay
que saber qué parte es consulta, qué parte es cálculo y qué parte pertenece a
`mercado_xm` frente a `monitoreo`.
Es la única pieza cuyo destino este mapa no resuelve del todo.

### `app/main.py` — 2 665 líneas

No es una app: son ~20 funciones `_run_*_seed` / `_run_*_backfill` y ~16
`_scheduled_*`. En Django cada una tiene destino propio y ninguno es `main`:

| Hoy | En Django |
|---|---|
| `_run_*_seed()` en `_deferred_init` | migración de seed (`0002_seed_*.py`) o `management/commands/` |
| `_run_*_backfill()` | `management/commands/`, ejecutado una vez |
| `_scheduled_*()` + `BackgroundScheduler` | task de Celery + `django_celery_beat`, con cola explícita |

Esto también resuelve `WORKERS=1`: hoy el scheduler vive dentro del proceso web y
por eso no se puede escalar. Con Celery el worker es otro servicio y la
restricción desaparece.

---

## 4. Por dónde empezar: `retos`

La primera rebanada tiene que probar el camino completo (modelo → servicio → API
→ tests → despliegue) con el mínimo de riesgo. `retos` es el candidato medido:

- 3 tablas (`retos_trimestre`, `retos_metrica`, `retos_valor_semanal`)
- 1 router (425 líneas), 1 servicio (265 líneas)
- **Su única FK sale hacia `usuarios`** — nada más del repo lo importa
- Lo importa exactamente un módulo: `app/api/v1/retos.py`

Es decir: se puede portar entero sin tocar ninguna otra tabla, y si sale mal no
arrastra nada. Después de `retos`, las hojas siguientes por el mismo criterio son
`polizas`, `starlink`, `informe_om` y `registros_cnd` — todas dependen solo de
`proyectos`.

### Orden completo sugerido

1. **Núcleo**: `plataforma` → `proyectos` → `clientes` → `fronteras`
2. **Piloto**: `retos` (valida el camino de punta a punta)
3. **Partir `models/contratos.py`** en sus 4 dominios — todavía en FastAPI, porque
   desbloquea todo el bloque de mercado
4. **Hojas**: `polizas`, `starlink`, `registros_cnd`, `arriendos`, `om`, `retos`,
   `garantias`, `mandatos`
5. **Bloque de mercado**: `ppa`, `facturacion`, `mercado_xm`, `liquidaciones`
6. **Los pesados**: `monitoreo`, `energia`, `contabilidad`
7. **`cumplimiento`** al final, cuando su destino esté decidido

---

## 5. Lo que este documento todavía no decide

- ~~**Dónde va `cumplimiento`.**~~ **DECIDIDO (2026-09-04):** va a `mercado_xm`,
  donde ya estaban sus tablas. Sus servicios viven en
  `apps/mercado_xm/services/cumplimiento/` (un módulo por tema) y su API en
  `api/v1/cumplimiento/`. Las 3 805 líneas del router se repartieron así: ~830
  puras que se movieron verbatim (`periodos`, `anual`, `xm_api`), ~340 de
  consultas reescritas al ORM (`consultas`, `piscinas`) y el resto, endpoints
  que ahora son funciones de servicio.
- **`liquidacion_mandatos` / `liquidacion_mandato_lineas`.** Están en
  `models/liquidaciones.py` pero pertenecen conceptualmente a `mandatos`. Acá se
  asignaron a `mandatos`; hay que confirmarlo con quien opera el módulo.
- **`rec_procesos`.** Una sola tabla, asignada a `mercado_xm` por afinidad; sin
  router propio, puede no valer una app.
- ~~**`cumplimiento` sigue sin desglosar**~~ — desglosado el 2026-09-04, ver arriba.
  Al portarlo desapareció el peor acoplamiento del repo: `clasificacion_energia`,
  `vista_contratos` y `balance_energia` llamaban a la VISTA
  `get_plantas_contratos(db=..., _=None)` para obtener datos. Ahora todos llaman a
  `piscinas.plantas_contratos()`, que es un servicio.
- **El destino de las 6 bases restantes.** `docs/UNERGY_DATABASE_ATLAS.md`
  documenta 6 bases y 511 tablas; este mapa cubre solo `operations`.
