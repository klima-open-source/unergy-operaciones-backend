# Unergy Operations Backend -- Full Discovery Document

> Generated 2026-05-18. Covers every file in the codebase: models, DDL migrations, API endpoints, schemas, services, config, deployment.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [All Tables (47)](#2-all-tables)
3. [Column Details per Table](#3-column-details-per-table)
4. [Foreign Key Relationships](#4-foreign-key-relationships)
5. [Indexes](#5-indexes)
6. [Enum Types (26)](#6-enum-types)
7. [All API Endpoints (100+)](#7-all-api-endpoints)
8. [Pydantic Schemas](#8-pydantic-schemas)
9. [Configuration Variables](#9-configuration-variables)
10. [Services & Background Jobs](#10-services--background-jobs)
11. [External Integrations](#11-external-integrations)
12. [Cross-Database Correlation](#12-cross-database-correlation)
13. [Deployment](#13-deployment)
14. [Seed Data](#14-seed-data)
15. [Built vs Missing Analysis](#15-built-vs-missing-analysis)

---

## 1. Architecture Overview

- **Framework:** FastAPI 0.115 + Uvicorn
- **ORM:** SQLAlchemy 2.0 + psycopg3 (binary)
- **Database:** PostgreSQL (Railway-hosted)
- **Auth:** JWT (HS256 via python-jose) + bcrypt password hashing
- **Migrations:** Alembic (directory exists) + `_PENDING_DDLS` in `app/main.py` (18 inline migrations) + `init_db.py` column additions
- **PDF Generation:** Playwright (Chromium headless)
- **Background Jobs:** APScheduler (MGS alarm polling every 15 min)
- **Deployment:** Railway (auto-deploy from master branch, `start.sh` runs init_db + alembic + uvicorn)
- **CORS:** Vercel subdomains (regex), localhost:5173, localhost:3000, configurable FRONTEND_URL

### Startup Sequence (`app/main.py` lifespan)

1. `_run_create_tables()` -- SQLAlchemy `Base.metadata.create_all()`
2. `_run_column_migrations()` -- 95+ DDL statements from `_PENDING_DDLS`
3. `_run_catalog_seed()` -- fault type catalog from `data/fallas_clasificadas_unergy.json`
4. `_run_tipo_migration()` -- re-points old snake_case fault codes to numeric codes
5. `_run_srv_operacion_sync()` -- marks `srv_operacion=True` for qualifying projects
6. MGS scheduler startup (if `MGS_ENABLED=True`)

---

## 2. All Tables

### From SQLAlchemy Models (30 tables)

| # | Table | Model File | Description |
|---|-------|-----------|-------------|
| 1 | `usuarios` | `usuarios.py` | Platform users (Unergy team) |
| 2 | `clientes` | `clientes.py` | Clients (solar project owners) |
| 3 | `portafolios` | `proyectos.py` | Project portfolios/groups |
| 4 | `proyectos` | `proyectos.py` | Solar projects (~40 columns) |
| 5 | `proyecto_info_tecnica` | `proyectos.py` | Technical info (panels, storage) |
| 6 | `proyecto_grupos_panel` | `proyectos.py` | Panel groups (brand/model/qty) |
| 7 | `proyecto_inversores` | `proyectos.py` | Inverter equipment |
| 8 | `proyecto_contactos` | `proyectos.py` | Project contacts for notifications |
| 9 | `proyecto_inversionistas` | `proyectos.py` | Investors (participation %) |
| 10 | `servicio_operacion` | `servicios.py` | Monitoring service config |
| 11 | `operacion_kpi` | `servicios.py` | Energy performance KPIs per period |
| 12 | `servicio_representacion` | `servicios.py` | XM market representation service |
| 13 | `representacion_gescon` | `servicios.py` | GESCON contract records |
| 14 | `servicio_cgm` | `servicios.py` | CGM (metering agent) service |
| 15 | `contratos_servicio` | `contratos.py` | Service contracts (ops/repr/rec) |
| 16 | `ppa_contratos` | `contratos.py` | Power Purchase Agreements |
| 17 | `ppa_tarifas` | `contratos.py` | Monthly tariff schedules per PPA |
| 18 | `ppa_compromisos_energia` | `contratos.py` | Monthly energy commitments per PPA |
| 19 | `contratos_arriendo` | `contratos.py` | Land lease contracts |
| 20 | `fronteras` | `fronteras.py` | Metering boundaries (70+ cols) |
| 21 | `frontera_lecturas` | `fronteras.py` | Hourly meter readings |
| 22 | `equipos` | `equipos.py` | Metering equipment |
| 23 | `equipo_sellos` | `equipos.py` | Tamper seals on equipment |
| 24 | `falla_cat_categorias` | `fallas.py` | Fault categories (9) |
| 25 | `falla_cat_tipos` | `fallas.py` | Fault types (with codes "1.1"-"9.x") |
| 26 | `falla_cat_estados` | `fallas.py` | Fault states (5: abierta->cerrada) |
| 27 | `falla_cat_prioridades` | `fallas.py` | Fault priorities (4) |
| 28 | `falla_cat_resoluciones` | `fallas.py` | Fault resolution types (8) |
| 29 | `fallas` | `fallas.py` | Fault tracking records |
| 30 | `falla_seguimientos` | `fallas.py` | Fault follow-up notes |
| 31 | `liquidaciones` | `liquidaciones.py` | Monthly settlements |
| 32 | `liquidacion_costos` | `liquidaciones.py` | Operational costs per settlement |
| 33 | `liquidacion_xm_datos` | `liquidaciones.py` | XM market data per frontera |
| 34 | `liquidacion_mandatos` | `liquidaciones.py` | Investor mandates |
| 35 | `liquidacion_mandato_lineas` | `liquidaciones.py` | 28 line items per mandate |
| 36 | `liquidacion_facturas` | `liquidaciones.py` | Service invoices |
| 37 | `reglas_contables` | `liquidaciones.py` | Accounting rules engine |
| 38 | `promoter_catalogo_requisitos` | `promotor.py` | Regulatory requirements (11 items) |
| 39 | `promoter_seguimiento` | `promotor.py` | Compliance tracking per project |
| 40 | `rec_procesos` | `rec.py` | REC certification processes |
| 41 | `rec_certificados` | `rec.py` | Renewable energy certificates |
| 42 | `documentos` | `documentos.py` | Polymorphic document storage |
| 43 | `mantenimientos` | `mantenimientos.py` | Maintenance records |

### From DDL Only (no SQLAlchemy model)

| # | Table | Created in | Description |
|---|-------|-----------|-------------|
| 44 | `generacion_diaria` | DDL migration 003 | Daily generation data per project |
| 45 | `monitoreo_verificaciones` | DDL migration 003 | 6-digit verification codes for client monitoring access |
| 46 | `gestion_registros` | DDL migration 007 | Project management records (PQR, preventivo, correctivo) |
| 47 | `cliente_servicios` | DDL migration 009 | Client service subscriptions |
| 48 | `cliente_documentos_comerciales` | DDL migration 009 | Client commercial documents |
| 49 | `asic_solicitudes` | DDL migration 012 | ASIC/GESCON contract requests |
| 50 | `asic_cambios_contratos` | DDL migration 012 | ASIC contract changes |
| 51 | `gescon_diccionario_contratos` | DDL migration 013 | GESCON contract dictionary |
| 52 | `ppa_contrato_proyectos` | DDL migration 011 | M2M join: PPA contracts <-> projects |
| 53 | `alarmas_monitoreo` | DDL migration 015 | MGS alarm records |
| 54 | `informes_guardados` | DDL migration 016 | Editorial workflow for operational reports |
| 55 | `clima_oni_monthly` | DDL migration 017 | ENSO climate index monthly |
| 56 | `clima_precip_monthly` | DDL migration 017 | Precipitation monthly by region |
| 57 | `clima_price_monthly` | DDL migration 017 | Energy price history monthly |
| 58 | `clima_forecasts` | DDL migration 017 | Climate forecast JSON snapshots |
| 59 | `precios_bolsa_diario` | DDL migration 018 | XM bolsa daily price aggregates |
| 60 | `precios_bolsa_horario` | DDL migration 018 | XM bolsa hourly prices |

**Note:** Tables 44-48 also have SQLAlchemy models (`generacion.py`, `clientes.py`, `gestion.py`, `informes.py`, `asic.py`) -- they are created via DDL first, then the ORM maps to them.

---

## 3. Column Details per Table

### `usuarios`
| Column | Type | Constraints |
|--------|------|------------|
| id | BIGSERIAL | PK |
| email | VARCHAR(255) | UNIQUE, NOT NULL |
| nombre | VARCHAR(255) | NOT NULL |
| rol | `rol_enum` | NOT NULL, default 'admin' |
| activo | BOOLEAN | NOT NULL, default TRUE |
| password_hash | VARCHAR(255) | NOT NULL |
| ultimo_acceso | TIMESTAMPTZ | |
| created_at | TIMESTAMPTZ | NOT NULL, default NOW() |
| updated_at | TIMESTAMPTZ | NOT NULL, default NOW() |

### `clientes`
| Column | Type | Constraints |
|--------|------|------------|
| id | BIGSERIAL | PK |
| razon_social_nombre | VARCHAR(500) | NOT NULL |
| nit_cedula | VARCHAR(20) | UNIQUE |
| tipo_persona | `tipo_persona_enum` | |
| representante_legal | VARCHAR(255) | |
| correo_electronico | VARCHAR(255) | |
| correo_liquidacion | VARCHAR(255) | Added DDL 006 |
| correo_monitoreo | VARCHAR(255) | Added DDL 006 |
| correo_soporte | VARCHAR(255) | Added DDL 006 |
| correo_operacional | VARCHAR(255) | Added DDL 015 |
| telefono_contacto | VARCHAR(50) | |
| direccion | VARCHAR(500) | |
| ciudad | VARCHAR(100) | |
| iva_pct | NUMERIC(5,2) | |
| retencion_pct | NUMERIC(5,2) | |
| reteica_pct | NUMERIC(5,2) | |
| rut_url | VARCHAR(1000) | Added init_db |
| created_at | TIMESTAMPTZ | NOT NULL, default NOW() |
| updated_at | TIMESTAMPTZ | NOT NULL, default NOW() |

### `proyectos`
| Column | Type | Constraints |
|--------|------|------------|
| id | BIGSERIAL | PK |
| cliente_id | BIGINT | FK clientes(id), NULLABLE (was NOT NULL, dropped) |
| portafolio_id | BIGINT | FK portafolios(id) |
| proyecto_padre_id | BIGINT | FK proyectos(id) self-ref |
| nombre_comercial | VARCHAR(255) | NOT NULL |
| nombre_bitacora | VARCHAR(255) | Added init_db |
| nombre_clientes | VARCHAR(255) | Added init_db |
| alias_monitoreo | TEXT | Added DDL 003 |
| topic_slug | VARCHAR(100) | |
| sub_project | VARCHAR(50) | Added init_db |
| clasificacion_regulatoria | `clasificacion_regulatoria_enum` | |
| tipo_tecnologia | `tipo_tecnologia_enum` | |
| tipo_proyecto | `tipo_proyecto_enum` | |
| potencia_instalada_kwp | NUMERIC(12,2) | |
| potencia_con_cen_mw | NUMERIC(10,4) | |
| cantidad_total_paneles | INTEGER | Added init_db |
| produccion_especifica_kwh_kwp | NUMERIC(10,2) | Added init_db |
| codigo_cnd | VARCHAR(50) | |
| estado | `estado_proyecto_enum` | NOT NULL, default 'en_desarrollo' |
| fecha_entrada_operacion | DATE | |
| departamento | VARCHAR(100) | |
| municipio | VARCHAR(100) | |
| direccion_vereda | TEXT | |
| latitud | NUMERIC(10,7) | |
| longitud | NUMERIC(10,7) | |
| tipo_conexion | VARCHAR(50) | |
| operador_red | VARCHAR(100) | |
| project_id_solenium | VARCHAR(100) | |
| carpeta_drive_codigo | VARCHAR(200) | |
| estado_resultados_url | VARCHAR(1000) | |
| income_distribution_method | VARCHAR(50) | |
| generar_liquidacion | BOOLEAN | default FALSE |
| p90_mensual_kwh | JSONB | Added DDL 004 |
| p50_mensual_kwh | JSONB | Added DDL 004 |
| codigo_tsf | VARCHAR(100) | Added DDL 005 |
| srv_operacion | BOOLEAN | NOT NULL, default FALSE |
| srv_representacion | BOOLEAN | NOT NULL, default FALSE |
| srv_cgm | BOOLEAN | NOT NULL, default FALSE |
| srv_ppa | BOOLEAN | NOT NULL, default FALSE |
| srv_promotor | BOOLEAN | NOT NULL, default FALSE |
| srv_rec | BOOLEAN | NOT NULL, default FALSE |
| origina_code | VARCHAR(100) | Added DDL cross-db |
| requestsdb_supply_id | BIGINT | Added DDL cross-db |
| quoia_node_name | VARCHAR(255) | Added DDL cross-db |
| created_at | TIMESTAMPTZ | NOT NULL, default NOW() |
| updated_at | TIMESTAMPTZ | NOT NULL, default NOW() |

### `fronteras`
| Column | Type | Constraints |
|--------|------|------------|
| id | BIGSERIAL | PK |
| proyecto_id | BIGINT | FK proyectos(id), NULLABLE |
| codigo_frontera | VARCHAR(50) | |
| nombre_frontera | VARCHAR(255) | NOT NULL |
| codigo_propio | VARCHAR(50) | |
| tipo_frontera | `tipo_frontera_enum` | NOT NULL |
| estado | `estado_frontera_enum` | NOT NULL, default 'activa' |
| fecha_registro_asic | DATE | |
| fecha_primer_registro_asic | DATE | |
| frontera_gemela_id | BIGINT | FK fronteras(id) self-ref |
| frontera_agrupada_id | BIGINT | FK fronteras(id) self-ref |
| frontera_embebida_id | BIGINT | FK fronteras(id) self-ref |
| registrada_por | VARCHAR(255) | DDL 013 |
| nit | VARCHAR(20) | DDL 013 |
| nivel_tension | INTEGER | DDL 013 |
| nivel_tension_kv | NUMERIC(10,2) | |
| transferencia_maxima_kwh | NUMERIC(14,3) | DDL 013 |
| representante_frontera | VARCHAR(255) | DDL 013 |
| fecha_inicio_representacion | DATE | DDL 013 |
| operador_red | VARCHAR(255) | DDL 013 |
| operador_red_zona | VARCHAR(255) | DDL 013 |
| nombre_cgm | VARCHAR(255) | DDL 013 |
| predio_id | VARCHAR(50) | DDL 013 |
| nombre_predio | VARCHAR(255) | DDL 013 |
| representante_ddv | VARCHAR(255) | DDL 013 |
| tipo_punto_medicion | INTEGER | |
| capacidad_transporte_mw | NUMERIC(10,4) | |
| capacidad_transporte_compartida_mw | NUMERIC(10,4) | |
| capacidad_efectiva_mw | NUMERIC(10,4) | |
| factor_perdidas | NUMERIC(10,6) | |
| clase_ct | VARCHAR(10) | |
| clase_pt | VARCHAR(10) | |
| nit_rf | VARCHAR(20) | |
| nit_cgm | VARCHAR(20) | |
| representante_anterior | VARCHAR(255) | |
| agente_exportador | VARCHAR(255) | |
| agente_importador | VARCHAR(255) | |
| nombre_recurso_generacion | VARCHAR(255) | |
| clasificacion_recurso | VARCHAR(100) | |
| niu | VARCHAR(50) | |
| consumo_promedio_mensual_mwh | NUMERIC(10,3) | |
| relacion_transformacion_ct | VARCHAR(100) | |
| relacion_transformacion_pt | VARCHAR(100) | |
| nro_serie_med_ppal | VARCHAR(100) | DDL 013 |
| marca_med_ppal | VARCHAR(100) | DDL 013 |
| modelo_med_ppal | VARCHAR(100) | DDL 013 |
| clase_medidor | VARCHAR(50) | DDL 013 |
| num_elementos_med_ppal | INTEGER | DDL 013 |
| fecha_cambio_med_ppal | DATE | DDL 013 |
| entidad_calibradora_med_ppal | VARCHAR(255) | DDL 013 |
| fecha_calibracion_med_ppal | DATE | DDL 013 |
| fecha_actualizacion_ppal | DATE | DDL 013 |
| nro_serie_med_resp | VARCHAR(100) | DDL 013 |
| marca_med_resp | VARCHAR(100) | DDL 013 |
| modelo_med_resp | VARCHAR(100) | DDL 013 |
| num_elementos_med_resp | INTEGER | DDL 013 |
| fecha_cambio_med_resp | DATE | DDL 013 |
| entidad_calibradora_med_resp | VARCHAR(255) | DDL 013 |
| fecha_calibracion_med_resp | DATE | DDL 013 |
| fecha_actualizacion_resp | DATE | DDL 013 |
| es_agrupadora | BOOLEAN | default FALSE |
| factor_psf | NUMERIC(10,6) | |
| es_principal_embebido | BOOLEAN | default FALSE |
| factor_acordado | NUMERIC(10,6) | |
| factor_ajuste | NUMERIC(10,6) | |
| factor_perdidas_frontera_principal | NUMERIC(10,6) | DDL 013 |
| municipio | VARCHAR(100) | |
| departamento | VARCHAR(100) | |
| centro_poblado | VARCHAR(100) | |
| direccion | VARCHAR(500) | |
| subestacion | VARCHAR(100) | |
| punto_conexion | VARCHAR(100) | |
| latitud | NUMERIC(10,7) | |
| longitud | NUMERIC(10,7) | |
| altitud_msnm | INTEGER | |
| codigo_sic_ddv | VARCHAR(50) | |
| codigo_sic_submercado_exportador | VARCHAR(50) | |
| codigo_sic_submercado_consumo | VARCHAR(50) | |
| codigo_sic_submercado_usuario | VARCHAR(50) | |
| codigo_sic_frontera_generacion | VARCHAR(50) | DDL 013 |
| codigo_sic_frontera_usuario | VARCHAR(50) | DDL 013 |
| potencia_maxima_declarada | NUMERIC(10,4) | DDL 013 |
| tipo_tecnologia | VARCHAR(100) | DDL 013 |
| codigo_ciiu | VARCHAR(20) | DDL 013 |
| clasificacion_industrial_general | VARCHAR(255) | DDL 013 |
| clasificacion_industrial_especifica | VARCHAR(255) | DDL 013 |
| fuente_lectura | `fuente_lectura_enum` | |
| created_at | TIMESTAMPTZ | NOT NULL, default NOW() |
| updated_at | TIMESTAMPTZ | NOT NULL, default NOW() |

### `fallas`
| Column | Type | Constraints |
|--------|------|------------|
| id | BIGSERIAL | PK |
| codigo_interno | VARCHAR(20) | UNIQUE, NOT NULL, auto-gen |
| codigo_legado | VARCHAR(30) | UNIQUE (partial) |
| proyecto_id | BIGINT | FK proyectos(id), NOT NULL |
| tipo_id | BIGINT | FK falla_cat_tipos(id), NOT NULL |
| estado_id | BIGINT | FK falla_cat_estados(id), NOT NULL |
| prioridad_id | BIGINT | FK falla_cat_prioridades(id), NOT NULL |
| resolucion_id | BIGINT | FK falla_cat_resoluciones(id) |
| registrado_por_id | BIGINT | FK usuarios(id), NOT NULL |
| asignado_a_id | BIGINT | FK usuarios(id) |
| descripcion | TEXT | NOT NULL |
| fecha_identificacion | DATE | NOT NULL |
| hora_identificacion | TIME | |
| fecha_ocurrencia | TIMESTAMPTZ | |
| fecha_resolucion | TIMESTAMPTZ | |
| sla_limite_horas | INTEGER | |
| sla_cumplido | BOOLEAN | |
| fotos_urls | JSONB | DDL 003 |
| centinela | VARCHAR(200) | DDL 003 |
| notificacion | BOOLEAN | NOT NULL, default FALSE. DDL 003 |
| created_at | TIMESTAMPTZ | NOT NULL, default NOW() |
| updated_at | TIMESTAMPTZ | NOT NULL, default NOW() |

### `liquidaciones`
| Column | Type | Constraints |
|--------|------|------------|
| id | BIGSERIAL | PK |
| proyecto_id | BIGINT | FK proyectos(id), NOT NULL |
| periodo | VARCHAR(7) | NOT NULL (YYYY-MM) |
| tipo_venta | `tipo_venta_liq_enum` | NOT NULL |
| estado | `estado_liquidacion_enum` | NOT NULL, default 'borrador' |
| estado_anterior | `estado_liquidacion_enum` | |
| fecha_cambio_estado | TIMESTAMPTZ | |
| ingresos_venta_energia | NUMERIC(18,2) | |
| ingresos_excedentes | NUMERIC(18,2) | |
| ingresos_rec | NUMERIC(18,2) | |
| ingresos_otros | NUMERIC(18,2) | |
| descuento_drs | NUMERIC(18,2) | |
| total_ingresos | NUMERIC(18,2) | |
| total_costos | NUMERIC(18,2) | |
| resultado_neto | NUMERIC(18,2) | |
| notas | TEXT | |
| estado_resultados_url | VARCHAR(1000) | DDL 010 |
| created_at | TIMESTAMPTZ | NOT NULL, default NOW() |
| updated_at | TIMESTAMPTZ | NOT NULL, default NOW() |
| UNIQUE | | (proyecto_id, periodo) |

### `ppa_contratos`
| Column | Type | Constraints |
|--------|------|------------|
| id | BIGSERIAL | PK |
| numero_codigo_contrato | VARCHAR(100) | UNIQUE |
| nombre_interno | VARCHAR(200) | DDL 011 |
| comprador_id | BIGINT | FK clientes(id). DDL 013 |
| vendedor_id | BIGINT | FK clientes(id). DDL 013 |
| comprador_nombre | VARCHAR(255) | DDL 011 |
| comprador_nit | VARCHAR(20) | DDL 011 |
| vendedor_nombre | VARCHAR(255) | DDL 011 |
| vendedor_nit | VARCHAR(20) | DDL 011 |
| fecha_inicio | DATE | |
| fecha_fin | DATE | |
| tarifa_base | NUMERIC(12,4) | |
| indice_indexacion | VARCHAR(50) | |
| periodicidad_indexacion | VARCHAR(50) | DDL 011 |
| periodo_indexacion_base | VARCHAR(7) | DDL 011 |
| valor_indexacion_base | NUMERIC(12,4) | DDL 011 |
| cantidad_minima_kwh_mes | NUMERIC(14,3) | DDL 011 |
| cantidad_maxima_kwh_mes | NUMERIC(14,3) | DDL 011 |
| periodicidad_facturacion | VARCHAR(50) | DDL 011 |
| tiempo_pago | INTEGER | DDL 011 |
| condiciones_pago | VARCHAR(500) | DDL 011 |
| codigo_sic | VARCHAR(50) | DDL 011 |
| gescon_codigo | VARCHAR(100) | DDL 011 |
| gescon_fecha_inicio | DATE | DDL 011 |
| gescon_fecha_fin | DATE | DDL 011 |
| gescon_precio | NUMERIC(12,4) | DDL 011 |
| gescon_cantidades_kwh | NUMERIC(14,3) | DDL 011 |
| created_at | TIMESTAMPTZ | NOT NULL, default NOW() |
| updated_at | TIMESTAMPTZ | DDL 011 |

### `precios_bolsa_diario` (DDL-only)
| Column | Type | Constraints |
|--------|------|------------|
| id | BIGSERIAL | PK |
| fecha | DATE | NOT NULL, UNIQUE |
| precio_promedio | REAL | NOT NULL |
| precio_min | REAL | |
| precio_max | REAL | |
| precio_escasez | REAL | |
| demanda_gwh | REAL | |
| hidro_pct | REAL | |
| termica_pct | REAL | |
| renovable_pct | REAL | |
| menor_pct | REAL | |
| hora_pico | INTEGER | |
| spread | REAL | |
| source_data | JSONB | |
| created_at | TIMESTAMPTZ | default NOW() |

### `precios_bolsa_horario` (DDL-only)
| Column | Type | Constraints |
|--------|------|------------|
| id | BIGSERIAL | PK |
| fecha | DATE | NOT NULL |
| hora | INTEGER | NOT NULL, CHECK 1-24 |
| precio_cop_kwh | REAL | NOT NULL |
| gen_hidro | REAL | |
| gen_termica | REAL | |
| gen_renovable | REAL | |
| gen_menor | REAL | |
| planta_marginal | VARCHAR(100) | |
| created_at | TIMESTAMPTZ | default NOW() |
| UNIQUE | | (fecha, hora) |

### `alarmas_monitoreo` (DDL-only)
| Column | Type | Constraints |
|--------|------|------------|
| id | BIGSERIAL | PK |
| proyecto_nombre | VARCHAR(255) | NOT NULL |
| severity | VARCHAR(20) | NOT NULL |
| alarm_type | VARCHAR(50) | NOT NULL |
| details | TEXT | NOT NULL |
| source_data | JSONB | |
| resolved_at | TIMESTAMPTZ | |
| created_at | TIMESTAMPTZ | NOT NULL, default NOW() |

### `informes_guardados` (DDL-only)
| Column | Type | Constraints |
|--------|------|------------|
| id | BIGSERIAL | PK |
| tipo | VARCHAR(20) | NOT NULL |
| sub_project | VARCHAR(200) | NOT NULL |
| periodo_desde | VARCHAR(10) | NOT NULL |
| periodo_hasta | VARCHAR(10) | NOT NULL |
| periodo_display | VARCHAR(100) | |
| proyecto_nombre | VARCHAR(300) | |
| html_content | TEXT | NOT NULL |
| charts_data | JSONB | |
| estado | VARCHAR(20) | NOT NULL, default 'borrador' |
| creado_por_id | BIGINT | FK usuarios(id) |
| editado_por_id | BIGINT | FK usuarios(id) |
| aprobado_por_id | BIGINT | FK usuarios(id) |
| creado_por_nombre | VARCHAR(255) | |
| editado_por_nombre | VARCHAR(255) | |
| aprobado_por_nombre | VARCHAR(255) | |
| creado_en | TIMESTAMPTZ | NOT NULL, default NOW() |
| editado_en | TIMESTAMPTZ | |
| aprobado_en | TIMESTAMPTZ | |
| correo_enviado | BOOLEAN | NOT NULL, default FALSE |
| correo_enviado_en | TIMESTAMPTZ | |
| UNIQUE | | (tipo, sub_project, periodo_desde, periodo_hasta) |

*(Remaining tables follow similar patterns -- all columns available in model files.)*

---

## 4. Foreign Key Relationships

```
clientes
  |-- proyectos.cliente_id
  |-- ppa_contratos.comprador_id
  |-- ppa_contratos.vendedor_id
  |-- contratos_servicio.contratante_id
  |-- contratos_servicio.prestador_id
  |-- proyecto_inversionistas.cliente_id
  |-- cliente_servicios.cliente_id
  |-- cliente_documentos_comerciales.cliente_id

portafolios
  |-- proyectos.portafolio_id

proyectos (self-ref: proyecto_padre_id)
  |-- proyecto_info_tecnica.proyecto_id
  |-- proyecto_grupos_panel.proyecto_id
  |-- proyecto_inversores.proyecto_id
  |-- proyecto_contactos.proyecto_id
  |-- proyecto_inversionistas.proyecto_id
  |-- fronteras.proyecto_id
  |-- fallas.proyecto_id
  |-- generacion_diaria.proyecto_id
  |-- liquidaciones.proyecto_id
  |-- contratos_servicio.proyecto_id
  |-- ppa_contrato_proyectos.proyecto_id (M2M join)
  |-- servicio_operacion.proyecto_id
  |-- operacion_kpi.proyecto_id
  |-- servicio_representacion.proyecto_id
  |-- servicio_cgm.proyecto_id
  |-- asic_solicitudes.proyecto_id
  |-- asic_cambios_contratos.proyecto_original_id
  |-- asic_cambios_contratos.proyecto_nuevo_id
  |-- gestion_registros.proyecto_id
  |-- documentos.entity_id (polymorphic)
  |-- mantenimientos.proyecto_id
  |-- rec_procesos.proyecto_id
  |-- promoter_seguimiento.proyecto_id
  |-- contratos_arriendo.proyecto_id

ppa_contratos
  |-- ppa_contrato_proyectos.contrato_id (M2M join)
  |-- ppa_tarifas.contrato_id
  |-- ppa_compromisos_energia.contrato_id

fallas
  |-- falla_seguimientos.falla_id

usuarios
  |-- fallas.registrado_por_id
  |-- fallas.asignado_a_id
  |-- falla_seguimientos.usuario_id
  |-- informes_guardados.creado_por_id
  |-- informes_guardados.editado_por_id
  |-- informes_guardados.aprobado_por_id

fronteras (self-ref: gemela/agrupada/embebida)
  |-- frontera_lecturas.frontera_id
  |-- equipos.frontera_id

equipos
  |-- equipo_sellos.equipo_id

asic_solicitudes
  |-- asic_cambios_contratos.solicitud_id

liquidaciones
  |-- liquidacion_costos.liquidacion_id
  |-- liquidacion_xm_datos.liquidacion_id
  |-- liquidacion_mandatos.liquidacion_id
  |-- liquidacion_facturas.liquidacion_id

liquidacion_mandatos
  |-- liquidacion_mandato_lineas.mandato_id

proyecto_inversionistas
  |-- liquidacion_mandatos.inversionista_id

cliente_servicios
  |-- cliente_documentos_comerciales.servicio_id
```

---

## 5. Indexes

### From DDL migrations
```sql
-- Fallas
ix_fallas_codigo_legado_unique ON fallas (codigo_legado) WHERE codigo_legado IS NOT NULL

-- Generacion
uq_generacion_proyecto_fecha ON generacion_diaria (proyecto_id, fecha)  -- UNIQUE
ix_generacion_proyecto_fecha ON generacion_diaria (proyecto_id, fecha)
ix_generacion_fecha ON generacion_diaria (fecha)

-- Monitoreo
ix_monitoreo_ver_email ON monitoreo_verificaciones (email)

-- Gestion
ix_gestion_proyecto ON gestion_registros (proyecto_id)
ix_gestion_tipo ON gestion_registros (tipo)

-- ASIC
ix_asic_codigo_sic ON asic_solicitudes (codigo_sic_contrato)
ix_asic_proyecto ON asic_solicitudes (proyecto_id)
ix_asic_estado_sic_fecha ON asic_solicitudes (estado_solicitud, codigo_sic_contrato, fecha_solicitud DESC NULLS LAST)

-- Alarmas
ix_alarmas_monitoreo_created ON alarmas_monitoreo (created_at DESC)
ix_alarmas_monitoreo_severity ON alarmas_monitoreo (severity) WHERE resolved_at IS NULL

-- Cross-DB
ix_proyectos_origina_code ON proyectos (origina_code) WHERE origina_code IS NOT NULL

-- Informes
uq_informes_tipo_sp_periodo ON informes_guardados (tipo, sub_project, periodo_desde, periodo_hasta)
ix_informes_sub_project ON informes_guardados (sub_project)
ix_informes_estado ON informes_guardados (estado)

-- Climate
ix_clima_oni_ym ON clima_oni_monthly (year, month)
ix_clima_precip_ym ON clima_precip_monthly (year, month, region)
ix_clima_price_ym ON clima_price_monthly (year, month)
ix_clima_forecasts_date ON clima_forecasts (forecast_date DESC)

-- Precios bolsa
ix_precios_bolsa_diario_fecha ON precios_bolsa_diario (fecha DESC)
ix_precios_bolsa_horario_fecha ON precios_bolsa_horario (fecha DESC, hora)
```

### From SQLAlchemy model definitions
```
-- Various implied by UNIQUE constraints and FK references
-- fallas.codigo_interno UNIQUE
-- clientes.nit_cedula UNIQUE
-- ppa_contratos.numero_codigo_contrato UNIQUE
-- liquidaciones.(proyecto_id, periodo) UNIQUE
-- gescon_diccionario_contratos.codigo_contrato UNIQUE
```

---

## 6. Enum Types

### From SQLAlchemy Models
| # | Enum | Values | Used In |
|---|------|--------|---------|
| 1 | `rol_enum` | admin, operaciones, monitoreo, liquidaciones, cgm, solo_lectura | usuarios.rol |
| 2 | `tipo_persona_enum` | natural, juridica | clientes.tipo_persona |
| 3 | `clasificacion_regulatoria_enum` | AGP, AGPE, AGGE, GD, DER, otra | proyectos.clasificacion_regulatoria |
| 4 | `tipo_tecnologia_enum` | solar_fotovoltaica, eolica, biomasa, pch, geotermica, otra | proyectos.tipo_tecnologia |
| 5 | `estado_proyecto_enum` | en_desarrollo, en_construccion, en_operacion, suspendido, cancelado | proyectos.estado |
| 6 | `tipo_proyecto_enum` | minigranja, autoconsumo, gd, movilidad_electrica, otro | proyectos.tipo_proyecto |
| 7 | `tipo_inversor_enum` | string, micro, central, hibrido | proyecto_inversores.tipo |
| 8 | `tipo_frontera_enum` | generacion, consumo, generacion_consumo, consumo_auxiliar, consumo_propio | fronteras.tipo_frontera |
| 9 | `estado_frontera_enum` | activa, inactiva, en_tramite, cancelada | fronteras.estado |
| 10 | `fuente_lectura_enum` | quoia, manual, excel, api | fronteras.fuente_lectura |
| 11 | `tipo_equipo_enum` | medidor_principal, medidor_respaldo, ct, pt, bornera | equipos.tipo |
| 12 | `servicio_aplica_enum` | representacion, operacion, rec | contratos_servicio.servicio_aplica |
| 13 | `estado_contrato_enum` | vigente, terminado, en_tramite | contratos_servicio.estado |
| 14 | `periodicidad_enum` | mensual, bimensual, trimestral, semestral, anual | contratos_servicio.periodicidad_pago |
| 15 | `estado_arriendo_enum` | activo, vencido, terminado | contratos_arriendo.estado |
| 16 | `tipo_venta_liq_enum` | bolsa, contrato, autoconsumo | liquidaciones.tipo_venta |
| 17 | `estado_liquidacion_enum` | borrador, en_revision, aprobado, liquidado | liquidaciones.estado |
| 18 | `tipo_costo_enum` | operacion, representacion, cgm, promotor, rec, arriendo, seguro, otro, cambio_equipos_medida | liquidacion_costos.tipo |
| 19 | `tipo_xm_dato_enum` | generacion, excedentes, consumo_propio, autoconsumo | liquidacion_xm_datos.tipo |
| 20 | `tipo_mandato_enum` | venta_energia, administracion, operacion, representacion, cgm, promotor, arriendo, otro | liquidacion_mandatos.tipo |
| 21 | `tipo_linea_mandato_enum` | ... (28 values, see model) ... | liquidacion_mandato_lineas.tipo |
| 22 | `tipo_factura_liq_enum` | emitida, recibida | liquidacion_facturas.tipo |

### From DDL migrations
| # | Enum | Values | Used In |
|---|------|--------|---------|
| 23 | `tipo_servicio_cliente_enum` | operacion, representacion, cgm, promotor | cliente_servicios.tipo |
| 24 | `tipo_documento_cliente_enum` | oferta, contrato, rut, certificado_bancario, camara_comercio | cliente_documentos_comerciales.tipo |
| 25 | `estado_documento_cliente_enum` | borrador, enviado, aceptado, firmado, rechazado | cliente_documentos_comerciales.estado |
| 26 | `tipo_solicitud_asic_enum` | registro, modificacion, terminacion, desistimiento | asic_solicitudes.tipo_solicitud |
| 27 | `estado_solicitud_asic_enum` | en_proceso, publicado, rechazado, desistido | asic_solicitudes.estado_solicitud |

---

## 7. All API Endpoints

All endpoints are under `/api/v1/` and require JWT authentication (`Depends(get_current_user)`) unless noted.

### Auth (`/api/v1/auth/`)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/token` | Login (email+password -> JWT) |
| GET | `/auth/me` | Current user info |
| GET | `/usuarios/` | List all users |
| POST | `/usuarios/` | Create user |
| PATCH | `/usuarios/{id}` | Update user |

### Clientes (`/api/v1/clientes/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/clientes/` | List (paginated, search, city filter) |
| POST | `/clientes/` | Create |
| GET | `/clientes/{id}` | Detail (with servicios + documentos) |
| PATCH | `/clientes/{id}` | Update |
| DELETE | `/clientes/{id}` | Delete |
| GET | `/clientes/{id}/servicios` | List services |
| POST | `/clientes/{id}/servicios` | Add service |
| DELETE | `/clientes/{id}/servicios/{sid}` | Remove service |
| GET | `/clientes/{id}/documentos` | List docs |
| POST | `/clientes/{id}/documentos` | Add doc |
| PATCH | `/clientes/{id}/documentos/{did}` | Update doc |
| DELETE | `/clientes/{id}/documentos/{did}` | Delete doc |
| POST | `/clientes/{id}/documentos/{did}/upload` | Upload file |

### Proyectos (`/api/v1/proyectos/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/proyectos/` | List (filter by estado, tipo_proyecto, portafolio, search) |
| POST | `/proyectos/` | Create |
| GET | `/proyectos/{id}` | Detail (with nested relations) |
| PATCH | `/proyectos/{id}` | Update |
| DELETE | `/proyectos/{id}` | Delete |
| PUT | `/proyectos/{id}/info-tecnica` | Upsert tech info |
| GET | `/proyectos/{id}/grupos-panel` | List panel groups |
| POST | `/proyectos/{id}/grupos-panel` | Add panel group |
| PUT | `/proyectos/{id}/grupos-panel/{gid}` | Update panel group |
| DELETE | `/proyectos/{id}/grupos-panel/{gid}` | Delete panel group |
| GET | `/proyectos/{id}/inversores` | List inverters |
| POST | `/proyectos/{id}/inversores` | Add inverter |
| PUT | `/proyectos/{id}/inversores/{iid}` | Update inverter |
| DELETE | `/proyectos/{id}/inversores/{iid}` | Delete inverter |
| GET | `/proyectos/{id}/contactos` | List contacts |
| POST | `/proyectos/{id}/contactos` | Add contact |
| PUT | `/proyectos/{id}/contactos/{cid}` | Update contact |
| DELETE | `/proyectos/{id}/contactos/{cid}` | Delete contact |
| GET | `/proyectos/{id}/inversionistas` | List investors |
| POST | `/proyectos/{id}/inversionistas` | Add investor |
| PUT | `/proyectos/{id}/inversionistas/{iid}` | Update investor |
| DELETE | `/proyectos/{id}/inversionistas/{iid}` | Delete investor |
| PUT | `/proyectos/{id}/servicios` | Toggle srv_* flags |

### Fallas (`/api/v1/fallas/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/fallas/catalogos` | All fault catalogs (estados, prioridades, tipos, resoluciones) |
| GET | `/fallas/` | List (filter by proyecto, estado, prioridad, tipo, search, date range) |
| POST | `/fallas/` | Create (auto-generates codigo_interno) |
| GET | `/fallas/stats/resumen` | Summary stats |
| GET | `/fallas/{id}` | Detail with seguimientos |
| PATCH | `/fallas/{id}` | Update |
| DELETE | `/fallas/{id}` | Delete |
| POST | `/fallas/{id}/seguimientos` | Add follow-up note |

### Generacion (`/api/v1/generacion/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/generacion/` | List (filter by proyecto, date range) |
| POST | `/generacion/` | Create single record |
| PUT | `/generacion/{id}` | Update |
| DELETE | `/generacion/{id}` | Delete |
| POST | `/generacion/bulk` | Bulk upsert (fuzzy name matching) |
| GET | `/generacion/resumen` | Summary per project |
| GET | `/generacion/por-proyecto/{pid}` | By project (date range) |

### Monitoreo (`/api/v1/monitoreo/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/monitoreo/fallas` | Fallas mapped to fallas-unergy state codes |
| PUT | `/monitoreo/fallas/{id}/estado` | Update via fallas-unergy |
| GET | `/monitoreo/generacion` | Generation for monitoring display |
| POST | `/monitoreo/verify-email` | Client email verification |
| POST | `/monitoreo/send-code` | Send 6-digit code |
| POST | `/monitoreo/verify-code` | Verify code |
| GET | `/monitoreo/getGeneration` | Legacy bridge |
| GET | `/monitoreo/getProjects` | Legacy bridge |
| GET | `/monitoreo/getPortfolios` | Legacy bridge |
| GET | `/monitoreo/getAllContratos` | Legacy bridge |
| GET | `/monitoreo/getFMOData/{pid}` | Solenium inverter proxy |
| POST | `/monitoreo/savePhoto` | Save photo URL to falla |
| POST | `/monitoreo/admin/sync-proyectos` | Sync projects from Unergy API |

### Liquidaciones (`/api/v1/liquidaciones/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/liquidaciones/catalogos/tipos` | Enum catalogs |
| GET | `/liquidaciones/` | List (filter by proyecto, periodo, estado) |
| POST | `/liquidaciones/` | Create |
| GET | `/liquidaciones/{id}` | Detail |
| PUT | `/liquidaciones/{id}` | Update |
| DELETE | `/liquidaciones/{id}` | Delete |
| POST | `/liquidaciones/{id}/limpiar` | Reset to borrador |
| POST/PUT | `/liquidaciones/{id}/costos` | Upsert cost |
| DELETE | `/liquidaciones/{id}/costos/{cid}` | Delete cost |
| POST/PUT | `/liquidaciones/{id}/mandatos` | Upsert mandate |
| DELETE | `/liquidaciones/{id}/mandatos/{mid}` | Delete mandate |
| POST/PUT | `/liquidaciones/{id}/mandatos/{mid}/lineas` | Upsert mandate line |
| DELETE | `/liquidaciones/{id}/mandatos/{mid}/lineas/{lid}` | Delete mandate line |
| POST/PUT | `/liquidaciones/{id}/facturas` | Upsert invoice |
| DELETE | `/liquidaciones/{id}/facturas/{fid}` | Delete invoice |
| GET | `/liquidaciones/vistas/por-proyecto` | View by project |
| GET | `/liquidaciones/vistas/por-inversionista` | View by investor |

### PPA (`/api/v1/ppa/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/ppa/` | List all PPA contracts |
| POST | `/ppa/` | Create |
| GET | `/ppa/{id}` | Detail (with proyectos, tarifas, compromisos) |
| PUT | `/ppa/{id}` | Update |
| DELETE | `/ppa/{id}` | Delete |
| GET | `/ppa/{id}/partes` | List comprador/vendedor details |
| PUT | `/ppa/{id}/tarifas` | Replace all monthly tariffs |
| PUT | `/ppa/{id}/compromisos` | Replace all monthly commitments |

### ASIC (`/api/v1/asic/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/asic/solicitudes` | List solicitudes |
| POST | `/asic/solicitudes` | Create |
| GET | `/asic/solicitudes/{id}` | Detail |
| PUT | `/asic/solicitudes/{id}` | Update |
| DELETE | `/asic/solicitudes/{id}` | Delete |
| GET | `/asic/solicitudes/{id}/cambios` | List cambios for solicitud |
| POST | `/asic/solicitudes/{id}/cambios` | Create cambio |
| DELETE | `/asic/solicitudes/{sid}/cambios/{cid}` | Delete cambio |
| GET | `/asic/gescon/diccionario` | List GESCON dictionary |
| POST | `/asic/gescon/diccionario` | Add entry |
| PUT | `/asic/gescon/diccionario/{id}` | Update entry |
| DELETE | `/asic/gescon/diccionario/{id}` | Delete entry |

### Fronteras (`/api/v1/fronteras/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/fronteras/` | List (filter by proyecto, tipo, estado) |
| POST | `/fronteras/` | Create |
| PUT | `/fronteras/{id}` | Upsert |

### Alertas (`/api/v1/alertas/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/alertas/contratos-ppa` | Orphan PPA detection + duplicate SIC detection |

### Contratos Servicio (`/api/v1/contratos-servicio/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/contratos-servicio/` | List (filter by proyecto, servicio_aplica, estado) |
| POST | `/contratos-servicio/` | Create (syncs client names from IDs) |
| GET | `/contratos-servicio/{id}` | Detail |
| PUT | `/contratos-servicio/{id}` | Update |
| DELETE | `/contratos-servicio/{id}` | Delete |

### Informes (`/api/v1/informes/`)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/informes/` | Upsert report |
| GET | `/informes/` | List (filter by tipo, sub_project, estado) |
| GET | `/informes/{id}` | Detail (includes html_content) |
| PATCH | `/informes/{id}/estado` | Change workflow state |
| DELETE | `/informes/{id}` | Delete (if not approved) |
| POST | `/informes/{id}/enviar` | Email approved report as PDF |

### Cumplimiento (`/api/v1/cumplimiento/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/cumplimiento/ppa` | List PPA contracts for selector |
| GET | `/cumplimiento/ppa/resumen` | All contracts compliance (year+month) |
| GET | `/cumplimiento/ppa/resumen-anual` | Annual commitment totals |
| GET | `/cumplimiento/simulador` | Simulator data (plants + GESCON + avg gen) |
| GET | `/cumplimiento/ppa/{id}/anual` | 12-month chart data |
| GET | `/cumplimiento/ppa/{id}` | Detailed monthly compliance |

### MGS (`/api/v1/mgs/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/mgs/status` | Current polling status + summary |
| GET | `/mgs/plants` | All monitored plants |
| GET | `/mgs/plants/{name}` | Single plant detail |
| GET | `/mgs/alarms` | Active alarms (filter by severity, type) |
| GET | `/mgs/alarms/history` | Alarm history (paginated) |
| POST | `/mgs/poll` | Force immediate poll |

### EVO Proxy (`/api/v1/evo/`)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/evo/dailyspot/latest` | Latest bolsa prices (persists to DB) |
| GET | `/evo/dailyspot/text` | Text summary of bolsa |
| GET | `/evo/dailyspot/history` | History from precios_bolsa_diario |
| GET | `/evo/dailyspot/hourly/{fecha}` | Hourly data from precios_bolsa_horario |
| GET | `/evo/clima/forecast` | Climate forecast (persists to DB) |
| GET | `/evo/clima/trading` | Trading recommendations |
| GET | `/evo/clima/history` | Forecast history from clima_forecasts |
| GET | `/evo/health` | EVO API health check |

### Correlation (`/api/v1/correlation/`)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/correlation/sync` | Run cross-database correlation |
| GET | `/correlation/project/{pid}` | Unified cross-DB view for a project |

### Solar (NOT REGISTERED in router.py)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/solar/proyectos` | XM SinergoX solar projects from Excel |
| GET | `/solar/filtros` | Filter values (municipio, departamento, estado) |
| GET | `/solar/generacion` | Daily generation from Excel |
| GET | `/solar/ranking` | Top N projects by generation |
| GET | `/solar/comparacion` | Compare XM vs internal DB projects |
| POST | `/solar/reload-cache` | Force Excel cache reload |

**NOTE:** `solar.py` has a router but it is NOT imported in `app/api/v1/router.py`. These endpoints are dead code. Also requires `openpyxl` which is not in `requirements.txt`.

### Root-level endpoints (not in router)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/monitoreo` | Serve fallas-unergy SPA |
| GET | `/monitoreo/` | Same (trailing slash) |

---

## 8. Pydantic Schemas

### `app/schemas/common.py`
- `PaginatedResponse[T]` -- generic paginated response (items, total, page, size, pages)
- `MsgResponse` -- simple `{msg: str}`

### `app/schemas/usuarios.py`
- `UsuarioOut`, `UsuarioCreate`, `UsuarioUpdate`, `TokenResponse`

### `app/schemas/clientes.py`
- `ClienteCreate`, `ClienteUpdate`, `ClienteBase`, `ClienteListOut`, `ClienteOut` (nested servicios + documentos)
- `ClienteServicioCreate`, `ClienteServicioOut`
- `ClienteDocumentoCreate`, `ClienteDocumentoUpdate`, `ClienteDocumentoOut`

### `app/schemas/proyectos.py`
- `ProyectoCreate`, `ProyectoUpdate`, `ProyectoOut` (nested inversiones, info_tecnica, grupos_panel, inversores, contactos)
- `ProyectoInfoTecnicaCreate`, `ProyectoInfoTecnicaOut`
- `ProyectoGrupoPanelCreate`, `ProyectoGrupoPanelUpdate`, `ProyectoGrupoPanelOut`
- `ProyectoInversorCreate`, `ProyectoInversorUpdate`, `ProyectoInversorOut`
- `ProyectoContactoCreate`, `ProyectoContactoUpdate`, `ProyectoContactoOut`
- `ProyectoInversionistaCreate`, `ProyectoInversionistaUpdate`, `ProyectoInversionistaOut`

### `app/schemas/fallas.py`
- `FallaCreate`, `FallaUpdate`, `FallaOut` (nested tipo, estado, prioridad, resolucion, seguimientos, proyecto)
- `FallaSeguimientoCreate`, `FallaSeguimientoOut`
- `FallaCatalogos` (all catalogs combined)
- `FallaCatEstadoOut`, `FallaCatPrioridadOut`, `FallaCatCategoriaOut`, `FallaCatTipoOut`, `FallaCatResolucionOut`

### `app/schemas/generacion.py`
- `GeneracionDiariaCreate`, `GeneracionDiariaUpdate`, `GeneracionDiariaOut`
- `GeneracionDiariaBulkItem`, `GeneracionDiariaBulkCreate`, `GeneracionDiariaBulkResult`
- `GeneracionResumenProyecto`

### `app/schemas/fronteras.py`
- `FronteraBase` (70+ fields), `FronteraCreate`, `FronteraOut`

### `app/schemas/ppa.py`
- `PPAContratoCreate`, `PPAContratoUpdate`, `PPAContratoOut` (nested tarifas, compromisos, comprador, vendedor, proyectos)
- `PPATarifaIn`, `PPATarifaOut`
- `PPACompromisoIn`, `PPACompromisoOut`

### `app/schemas/contratos_servicio.py`
- `ContratoServicioCreate`, `ContratoServicioUpdate`, `ContratoServicioOut` (nested contratante, prestador)

### `app/schemas/asic.py`
- `AsicSolicitudCreate`, `AsicSolicitudOut`
- `AsicCambioCreate`, `AsicCambioOut`
- `GesconDiccionarioCreate`, `GesconDiccionarioOut`

### `app/schemas/mgs.py`
- `AlarmOut`, `PlantOut`, `StatusCountsOut`, `SummaryOut`, `MGSStatusOut`

### Inline schemas (in endpoints)
- `informes.py`: `InformeUpsertIn`, `EstadoIn`, `InformeOut`, `InformeDetailOut`
- `cumplimiento.py`: No schemas (returns raw dicts)

---

## 9. Configuration Variables

### `app/core/config.py` (Settings class)

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `DATABASE_URL` | str | (required) | PostgreSQL connection string |
| `SECRET_KEY` | str | "secret" | JWT signing key |
| `JWT_EXPIRE_MINUTES` | int | 480 | Token expiry (8 hours) |
| `ALGORITHM` | str | "HS256" | JWT algorithm |
| `STORAGE_BACKEND` | str | "local" | File storage backend |
| `STORAGE_LOCAL_PATH` | str | "uploads" | Local upload directory |
| `ENVIRONMENT` | str | "production" | Environment name |
| `APP_NAME` | str | "Plataforma Operaciones Unergy" | App display name |
| `FRONTEND_URL` | str | "http://localhost:5173" | CORS origin |
| `TIMEZONE` | str | "America/Bogota" | Default timezone |
| `UNERGY_API_URL` | str | "https://api.unergy.io" | Unergy platform API |
| `UNERGY_ACCOUNT_ID` | str | "" | Unergy account ID |
| `UNERGY_LOGIN` | str | "" | Unergy login |
| `UNERGY_PASSWORD` | str | "" | Unergy password |
| `SOLENIUM_AUTH_URL` | str | "https://auth.sole.tech/api" | Solenium auth URL |
| `SOLENIUM_DATA_URL` | str | "https://data.sole.tech/api" | Solenium data URL |
| `SOLENIUM_USER` | str | "" | Solenium username |
| `SOLENIUM_PASS` | str | "" | Solenium password |
| `QUOIA_BASE_URL` | str | "https://api.quoia.co/api" | Quoia CGM API |
| `QUOIA_API_TOKEN` | str | "" | Quoia API token |
| `MGS_ENABLED` | bool | True | Enable MGS polling |
| `MGS_POLL_INTERVAL_MINUTES` | int | 15 | Polling interval |
| `EVO_API_URL` | str | "" | EVO energy API (via Tailscale) |
| `EVO_API_TOKEN` | str | "" | EVO API token |
| `ORIGINA_DATABASE_URL` | str | "" | OriginabotDB read-only connection |
| `REQUESTSDB_DATABASE_URL` | str | "" | RequestsDB read-only connection |
| `SMTP_HOST` | str | "" | SMTP server |
| `SMTP_PORT` | int | 587 | SMTP port |
| `SMTP_USER` | str | "" | SMTP username |
| `SMTP_PASSWORD` | str | "" | SMTP password |
| `SMTP_FROM` | str | "operaciones@unergy.io" | From email address |

---

## 10. Services & Background Jobs

### MGS Alarm System (`app/services/mgs/`)
- **Scheduler:** APScheduler background job, polls every 15 min
- **QuoiaClient:** Fetches all meter nodes from Quoia CGM API
- **SoleniumClient:** JWT-authenticated Solenium API (inverter availability/state)
- **SoleniumChecker:** Cross-references Quoia nodes with Solenium inverter status
- **AlarmEngine:** Evaluates nodes, generates alarms:
  - `PLANTA_CAIDA` (CRITICAL) -- 4 consecutive bad polls (~30 min)
  - `SIN_GENERACION` (WARNING) -- meter OK but zero generation during solar hours
  - `CORTE_ZONA` (CRITICAL) -- 2+ projects on same circuit/substation down
  - `INVERSORES_DEGRADADOS` -- inverter state != Grid-connected
  - `RECUPERACION` (INFO) -- project restored
- **GridMap:** Hardcoded mapping of 38 projects to electrical grid hierarchy (OR -> Subestacion -> Circuito)

### Correlation Service (`app/services/correlation.py`)
- Cross-database fuzzy name matching between operaciones, originabotdb, requestsdb
- Updates `proyectos.origina_code` and `proyectos.requestsdb_supply_id`

### Email Service (`app/services/email_service.py`)
- Playwright Chromium headless PDF generation from HTML
- SMTP email sending with PDF attachment
- Used by informes workflow

### Project Matching (`app/utils/proyecto_matching.py`)
- 5-level fuzzy matching: exact -> alias -> alt names -> partial containment -> SequenceMatcher (>=0.75)
- Used by bulk generation import

---

## 11. External Integrations

| System | Protocol | Purpose |
|--------|----------|---------|
| **Unergy API** (api.unergy.io) | REST + JWT | Project generation data, project sync |
| **Solenium** (auth.sole.tech / data.sole.tech) | REST + JWT (access+refresh) | FMO inverter availability, state |
| **Quoia CGM** (api.quoia.co) | REST + Token | Meter node status, real-time readings |
| **EVO Energy** (Tailscale) | REST + X-EVO-Token | DailySpot bolsa prices, climate forecasts |
| **OriginabotDB** | PostgreSQL (read-only) | Commercial data, supply requests |
| **RequestsDB** | PostgreSQL (read-only) | Grid infrastructure (transformers, circuits, substations) |
| **SMTP** | Email | Operational report delivery |

---

## 12. Cross-Database Correlation

Three databases track the same solar projects:

```
operaciones (this DB)          originabotdb              requestsdb
  proyectos                      minifarm_project           supplies_supplyrequest
    .origina_code  <-------->    .name (code)               .project_name
    .requestsdb_supply_id <-->                              .id
    .quoia_node_name <------->   (Quoia API node names)
```

Correlation strategy: fuzzy name matching + frontera code matching.
`POST /api/v1/correlation/sync` runs the correlation.

---

## 13. Deployment

### Railway
- **File:** `railway.toml` (not found -- auto-detected Dockerfile)
- **Dockerfile:** python:3.12-slim + playwright chromium
- **start.sh:** `python init_db.py && alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- **`.env.example`:** 28 env vars documented
- **`backend.md`:** Developer guide (auto-deploy from master)
- **DB pool:** pool_size=10, max_overflow=20, pool_pre_ping=True

### SPA hosting
- Static files mounted at `/static/uploads`
- fallas-unergy SPA served from `static/monitoreo/`

---

## 14. Seed Data

### `app/seeds/seed_data.py`
- **8 users** (Unergy team: Juan Jose, Laura, Jessica, Nicolas, Eduardo, Victor, Camilo, Daniel)
- **7 fault categories** (inversor, comunicacion, produccion, red, estructura, medicion, otro)
- **16 fault types** (inv_falla_total, com_perdida, prod_baja_pr, etc.)
- **5 fault states** (abierta, en_gestion, en_espera, cerrada, sin_solucion)
- **4 priorities** (critica, grave, media, leve)
- **8 resolutions** (reinicio_inversor, visita_tecnica, cambio_componente, etc.)
- **11 promoter regulatory requirements** (CREG CND registration, AGGE/GD requirements)

### Startup catalog seed (from JSON file)
- 9 expanded fault categories with icons/colors (codigo "1"-"9")
- Fault types from `data/fallas_clasificadas_unergy.json` with numeric codes ("1.1", "2.8", etc.)
- Old snake_case fault codes are migrated to numeric codes at startup

---

## 15. Built vs Missing Analysis

### Fully Built & Operational
- **Project management:** Full CRUD with nested technical info, panel groups, inverters, contacts, investors
- **Client management:** Full CRUD with 5 email types, services, documents, file upload
- **Fault tracking:** Complete lifecycle with catalogs, SLA, photos, follow-ups, legacy migration
- **PPA contracts:** M2M with projects, monthly tariffs and energy commitments
- **ASIC/GESCON:** Contract requests, changes, dictionary, temporal DISTINCT ON queries
- **Liquidaciones:** Complete settlement workflow with costs, mandates (28 line types), invoices, XM data
- **Energy compliance:** Real-time generation from Unergy API, GESCON-resolved plant assignments, deficit/surplus
- **MGS monitoring:** Quoia + Solenium polling, alarm engine with zone outage detection, grid map
- **EVO proxy:** Bolsa prices + climate forecasts with DB persistence
- **Operational reports:** Editorial workflow (borrador -> revisado -> aprobado), PDF generation, email delivery
- **Cross-DB correlation:** operaciones <-> originabotdb <-> requestsdb fuzzy matching
- **Auth & security:** JWT, role-based users, verification codes for client monitoring access

### Partially Built / Gaps
| Gap | Details |
|-----|---------|
| **solar.py NOT WIRED** | `app/api/v1/solar.py` has 6 endpoints for XM SinergoX data but is NOT imported in `router.py`. Dead code. Also needs `openpyxl` added to requirements.txt. |
| **Alembic migrations empty** | `start.sh` runs `alembic upgrade head` but all real migrations are in `_PENDING_DDLS` and `init_db.py`. Alembic directory likely has no meaningful versions. |
| **No DDL models for some tables** | `generacion_diaria`, `monitoreo_verificaciones`, `gestion_registros` have SQLAlchemy models but their CREATE TABLE comes from DDL. Others (`alarmas_monitoreo`, `clima_*`, `precios_bolsa_*`) are DDL-only -- no ORM model exists. |
| **Fronteras CRUD incomplete** | Only list + create + upsert. No delete, no detail endpoint. |
| **Equipos no endpoints** | Equipment and seal models exist but no API endpoints. |
| **Documentos no endpoints** | Polymorphic document model exists but no dedicated CRUD (only via client documents). |
| **Mantenimientos no endpoints** | Maintenance model exists but no API endpoints. |
| **REC no endpoints** | REC process and certificate models exist but no API endpoints. |
| **Promoter no endpoints** | Catalog and tracking models exist but no API endpoints. |
| **Contratos arriendo no endpoints** | Land lease model exists but no API endpoints. |
| **Frontera lecturas no endpoints** | Hourly reading model exists but no API endpoints. |
| **ServicioOperacion KPIs limited** | Model exists but no dedicated CRUD endpoints. |
| **No file storage abstraction** | `STORAGE_BACKEND=local` is the only implementation. No S3/GCS. |
| **No rate limiting** | No request rate limiting on any endpoint. |
| **No pagination on many list endpoints** | Some endpoints (clientes, fallas) paginate; many others return all rows. |
| **No WebSocket** | Real-time monitoring uses polling, not push. |
| **No audit log** | No record of who changed what, when. |
| **No soft delete** | Hard deletes across all endpoints. |
| **No background task queue** | Only APScheduler for MGS; no Celery/Redis for async tasks. |
| **Climate tables unused by backend** | `clima_oni_monthly`, `clima_precip_monthly`, `clima_price_monthly` have DDL but no ingest endpoints. Populated by EVO only via `clima_forecasts`. |

### Schema Debt
- 95+ inline DDL statements in `_PENDING_DDLS` instead of proper Alembic migrations
- `init_db.py` has its own set of column additions that overlap with `_PENDING_DDLS`
- Enum types created via DDL cannot be easily dropped/recreated (PostgreSQL limitation)
- No foreign key from `informes_guardados` to `proyectos` (joined by `sub_project` string)
- No constraint on `alarmas_monitoreo.severity` or `alarm_type` values
- `precios_bolsa_*` and `clima_*` tables have no FK to projects or any other table
- `ppa_contrato_proyectos` join table created in DDL but also defined as SQLAlchemy `Table` in `contratos.py`

---

*End of discovery document. 60 tables, 26+ enum types, 100+ API endpoints, 30+ config variables documented.*
