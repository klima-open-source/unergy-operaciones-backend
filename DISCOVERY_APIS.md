# External API Discovery

**Date:** 2026-05-18

Three external APIs serve Unergy's operations platform:
1. **Quoia** -- Smart meter (frontera) data for Minigranjas
2. **Solenium** -- Inverter monitoring, availability, relay, weather
3. **Unergy API** -- Internal Django operations platform (XM invoicing, settlements, financial models)

---

## 1. Quoia Smart Meter API

**Base URL:** `https://gaia.quoia.energy/api`
**Auth:** Token-based (`Authorization: Token <token>`)
**Token:** `${QUOIA_API_TOKEN}` (env var)

### Available Endpoints

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/meter/` | 200 | Paginated list of all meters (300 total) |
| GET | `/meter/?search=<term>` | 200 | Search meters (searches all fields, not just name) |
| GET | `/meter/?archived=false` | 200 | Filter by archived status |
| GET | `/node/` | 200 | Returns `[]` (empty -- nodes may be deprecated) |
| GET | `/measurement/typical_curve/` | 200 | All typical consumption curves (1,101 entries) |
| GET | `/measurement/typical_curve/?node=<id>` | 200 | Typical curves for specific meter (7 per meter = 7 weekdays) |

### Meter List Response

**Pagination:** Django REST Framework standard (`count`, `next`, `previous`, `results`)
**Page size:** Default ~20 per page

```json
{
  "count": 300,
  "next": "https://gaia.quoia.energy/api/meter/?page=2",
  "results": [
    {
      "id": 1662,
      "name": "MGS 0080 - Chinu Sur 4 Principal",
      "serial": "88866407",
      "archived": false,
      "available": false
    }
  ]
}
```

### Meter Statistics

| Category | Count |
|----------|-------|
| Total meters | 300 |
| Minigranja (prefix `Minigranja`) | 37 |
| MGS (prefix `MGS`) | 33 |
| GD (generacion distribuida) | 25 |
| Consumo (consumption) | 10 |
| Archived | 18 |
| Available | 18 |

**Name patterns:** `Minigranja NNNN - Name Principal/Respaldo`, `MGS NNNN - Name Principal`, `GD NNN - Name`, `Consumo Name`, `Generacion Name`, `Test ...`, `Taurus ...`, `Fuente ...`

### Typical Curve Response

Each meter has 7 typical curves (one per weekday, 0=Monday through 6=Sunday). Each curve contains 24-hour energy profiles at 15-minute resolution (96 points).

```json
{
  "id": 1051435,
  "weekday": 6,
  "active": true,
  "auto": true,
  "source": "AUTO",
  "status": "ACTIVE",
  "lock_mode": "NONE",
  "lock_until": null,
  "quality_score": 1.0,
  "days_used": 10,
  "hours_coverage": 1.0,
  "iae": [2.53, 2.53, 2.53, ...],  // 96 values: Imported Active Energy (kWh per 15min)
  "eae": [0.0, 0.0, 0.0, ...],      // 96 values: Exported Active Energy (kWh per 15min)
  "created_at": "2026-05-18T15:01:29.597484Z",
  "updated_at": "2026-05-18T15:01:29.611680Z",
  "valid_from": "2026-05-18T15:00:00.079657Z",
  "valid_to": null,
  "company": <int>,
  "node": 1662,
  "created_by": null,
  "update_by": null
}
```

**Fields:**
- `iae[]`: Imported Active Energy -- 96 values for 24 hours at 15-min intervals (kWh consumed)
- `eae[]`: Exported Active Energy -- 96 values (kWh exported/injected to grid)
- `quality_score`: 0.0 to 1.0 (data completeness)
- `hours_coverage`: fraction of day covered by measurements
- `days_used`: how many days of data used to compute the curve

### Endpoints Confirmed NOT Available

`/readings/`, `/data/`, `/energy/`, `/alerts/`, `/status/`, `/devices/`, `/installations/`, `/report/`, `/billing/`, `/gateway/`, `/customer/`, `/account/` -- all return 404.

### Notes

- Meter detail endpoint (`/meter/<id>/`) returns 500 (server bug)
- The `/node/` endpoint returns empty -- the system uses `/meter/` instead
- No real-time readings endpoint discovered; typical curves are the primary data format
- The `node` field in typical_curve corresponds to `meter.id`
- Quoia meters link to Solenium projects via the Unergy API `meter.quoia_node_id` field

---

## 2. Solenium API (Inverter Monitoring)

**Auth URL:** `https://auth.sole.tech/api`
**Data URL:** `https://data.sole.tech/api`
**Auth:** JWT (username/password -> access + refresh tokens)
**Credentials:** `${SOLENIUM_USER}` / `${SOLENIUM_PASSWORD}` (env vars)
**Token lifetime:** ~5 minutes (refresh before expiry)
**OpenAPI Schema:** Available at `GET /api/schema/` (YAML)

### Authentication

```python
POST https://auth.sole.tech/api/token/
{"username": "${SOLENIUM_USER}", "password": "${SOLENIUM_PASSWORD}"}
# Response: {"access": "eyJ...", "refresh": "eyJ..."}

POST https://auth.sole.tech/api/token/refresh/
{"refresh": "<refresh_token>"}
# Response: {"access": "eyJ..."}
```

### Available Endpoints (from OpenAPI schema)

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/project/` | List all projects (74 total, 35 minifarms) |
| GET | `/api/project/{id}/` | Project detail (includes grid_voltage, panel_quantity, inverter_power) |
| GET | `/api/project/{id}/inverter/` | List inverters with live state/power/temperature |
| GET | `/api/project/{id}/power/` | 5-minute power timeseries per inverter (today) |
| GET | `/api/project/{id}/energy/?granularity=day\|hour\|month&date_from=&date_to=` | Energy generation (MWh) |
| GET | `/api/project/{id}/generation/?start_date=&end_date=` | Hourly generation (kWh) |
| GET | `/api/project/{id}/measurement/?variable=cp1\|cp2\|cp3&time_scale=0` | Inverter measurement timeseries |
| GET | `/api/project/{id}/measurements-dc/` | DC voltage/current per string (5-min) |
| GET | `/api/project/{id}/weather/?date_from=&date_to=` | Weather station data |
| GET | `/api/project/{id}/relay/` | Reconnector live status (voltage, current, frequency) |
| GET | `/api/project/{id}/relay/historical/?start_date=&end_date=&vars=` | Relay historical data |
| POST | `/api/project/{id}/relay/set-status/` | Control relay (open/close) |
| POST | `/api/project/{id}/relay/set-status-third/` | Third-party relay control |
| GET | `/api/project/{id}/performance_ratio/` | Performance ratio (current) |
| GET | `/api/project/{id}/performance_ratio/historical/` | PR historical |
| GET | `/api/project/{id}/quoia_measurements/` | Quoia meter readings for project |
| GET | `/api/project/{id}/quoia_measurements_history/` | Quoia historical (15-min kWh) |
| GET/POST | `/api/project/{id}/exclusions/` | Availability exclusion periods |
| GET | `/api/project/{id}/exclusions/types/` | 9 exclusion type definitions |
| GET | `/api/project/{id}/tcu_status/` | DAQ (data acquisition) device status |
| GET | `/api/project_availability/` | All projects availability categorized |
| GET | `/api/project_availability_detail/{id}/` | Single project availability |
| GET | `/api/project_summary/` | Fleet summary (power, irradiance per project) |
| GET/POST | `/api/project_detail/{id}/` | Extended project detail (grid operator, panels, inverters) |
| GET/POST | `/api/project_detail/{name}/reference_voltage/` | Reference voltage config |
| GET/POST | `/api/project_detail/check_for_project/` | Check if project exists |
| GET | `/api/company_projects/` | Projects for authenticated company |
| GET | `/api/inverter/{id}/?date_from=&date_to=&variable=` | Inverter historical data |
| GET | `/api/docs/` | Platform documentation (KPI definitions) |
| GET | `/api/docs/{doc_id}/` | Specific document |

### Auth API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/user/` | All platform users (545 total, paginated) |

### Project List Response

```json
{
  "id": 164,
  "name": "Ladrillera Del Meta",
  "lon": -73.746618,
  "lat": 4.025222,
  "plant_code": "NE=46651886",
  "weather_plant_code": "SOLARMAN-002502005196-001",
  "is_minifarm": true,
  "is_self_consumption": false,
  "installed_capacity": 1622.4,
  "location": "Acacias, Meta"
}
```

### Project Detail Response (extended)

```json
{
  "id": 164,
  "name": "Ladrillera Del Meta",
  "lon": -73.746618,
  "lat": 4.025222,
  "plant_code": "NE=46651886",
  "weather_plant_code": "SOLARMAN-002502005196-001",
  "is_minifarm": true,
  "installed_capacity": 1622.4,
  "location": "Acacias, Meta",
  "grid_voltage": 800,
  "grid_operator": "EMSA",
  "panel_quantity": 2496,
  "panel_power": 0.65,
  "inverter_power": "300",
  "inverter_quantity": 4,
  "generation": null
}
```

### Inverter Status Response

```json
{
  "results": [
    {
      "id": 2303,
      "dev_name": "330KTL-Inversor1",
      "state": "Standby: no irradiation",
      "power": 0.0,
      "efficiency": 0.0,
      "temperature": 43.2,
      "time": "2026-05-18 22:50:03"
    }
  ]
}
```

**Inverter states observed:** `Grid-connected`, `Standby: no irradiation`, `Shutdown`, `Fault`, `Disconnected`, `Stop`

### Inverter Detail Variables

When querying `/api/inverter/{id}/?date_from=&date_to=&variable=`:

Available variables: `ab_u`, `app1`, `app2`, `app3`, `bc_u`, `ca_u`, `cp1`, `cp2`, `cp3`, `pfp1`, `pfp2`, `pfp3`, `vp1`, `vp2`, `vp3`

```json
{
  "inverter_id": 2303,
  "inverter_name": "330KTL-Inversor1",
  "date_from": "2026-05-17",
  "date_to": "2026-05-18",
  "variables": ["cp1"],
  "count": 427,
  "data": [
    {"time": "2026-05-17 05:50:03", "cp1": 0.0},
    {"time": "2026-05-17 06:00:04", "cp1": 1.512}
  ]
}
```

### Power Timeseries Response

`GET /api/project/{id}/power/` -- 5-minute intervals, per inverter:

```json
{
  "unit": "kW",
  "power": {
    "330KTL-Inversor1": {
      "2026-05-18 00:00": 0.0,
      "2026-05-18 06:00": 78.5,
      "2026-05-18 12:00": 312.1
    }
  }
}
```

### Generation Response

`GET /api/project/{id}/generation/?start_date=2026-05-01&end_date=2026-05-18`:

```json
{
  "project_id": 164,
  "project_name": "Ladrillera Del Meta",
  "total_generation_kwh": 94350.62,
  "generation_kwh": {
    "2026-05-01 06:00": 78.36,
    "2026-05-01 09:00": 1156.29,
    "2026-05-01 12:00": 1146.15
  }
}
```

### DC Measurements Response

`GET /api/project/{id}/measurements-dc/` -- per string voltage/current:

```json
{
  "330KTL-Inversor1": {
    "vs1": {"2026-05-18 06:00": 1029.5, "2026-05-18 06:05": 1080.8},
    "vs2": {"2026-05-18 06:00": 1027.1},
    "is1": {"2026-05-18 06:00": 3.2}
  }
}
```

### Weather Response

`GET /api/project/{id}/weather/?date_from=2026-05-17&date_to=2026-05-18`:

Returns time-series dict per variable: `wind_direction`, and likely `temperature`, `irradiance`, `humidity` (only wind_direction returned for this project).

### Relay (Reconnector) Response

`GET /api/project/{id}/relay/` (project 113 has relay):

```json
{
  "time": "2026-05-18 22:59:22",
  "i_a": 0, "i_b": 0, "i_c": 0, "i_n": 0,
  "u_a": 7900, "u_b": 7600, "u_c": 7500,
  "u_r": 7900, "u_s": 7600, "u_t": 7500,
  "f_abc": 59.96,
  "pf": 0,
  "v_three_phase": 13279.1,
  "p_three_phase": 0,
  "active": true
}
```

### Quoia Measurements History Response

`GET /api/project/{id}/quoia_measurements_history/` (project 127):

```json
{
  "2026-05-18 00:00:00": {"value": 0.0, "unit": "kWh"},
  "2026-05-18 00:15:00": {"value": 0.0, "unit": "kWh"},
  "2026-05-18 06:15:00": {"value": 12.4, "unit": "kWh"}
}
```

15-minute intervals, energy in kWh.

### Project Availability Response

```json
{
  "total": 35,
  "categories": [
    {"id": "high", "label": "Mayor a 90%", "count": 0, "value": 0.0, "color": "#22c55e", "items": []},
    {"id": "medium", "label": "Entre 66% y 90%", "count": 0, "items": []},
    {"id": "low", "label": "Entre 33% y menor a 66%", "count": 0, "items": []},
    {"id": "critical", "label": "Menor a 33%", "count": 0, "items": []},
    {"id": "disconnect", "label": "Faltan datos", "count": 35, "items": [
      {"project": 164, "name": "Ladrillera Del Meta", "availability": null}
    ]}
  ]
}
```

**Currently all 35 projects show `disconnect` (missing data) for availability.**

### Exclusion Types

| ID | Description |
|----|-------------|
| 1 | Grid failure, electrical disturbance, OR-mandated power reductions |
| 2 | Force majeure events |
| 3 | External authority inspections |
| 4 | Client-caused interruptions (not contractor negligence) |
| 5 | POA < 50 W/m2 periods |
| 6 | OR-imposed setpoint restrictions |
| 7 | Communication error (data not recovered) |
| 8 | Power factor below 0.95 |
| 9 | Corrective/preventive maintenance |

### Project Summary Response (Fleet Overview)

```json
{
  "generated_at": "2026-05-18 22:54:17",
  "count": 35,
  "items": [
    {
      "project_id": 164,
      "project_name": "Ladrillera Del Meta",
      "power_kw": 0.0,
      "power_time": "2026-05-18 22:50:03",
      "irradiance_w_m2": null,
      "irradiance_time": null,
      "irradiance_source": null,
      "frontier_generation_kwh": null,
      "frontier_generation_time": null
    }
  ]
}
```

### Project Inventory

**35 Minifarms:**

| ID | Name | Location | Capacity (kWp) |
|----|------|----------|----------------|
| 122 | Minigranja 0001 - Uruaco | Luruaco | 1,379.8 |
| 136 | Minigranja 0002 - Baraya | Baraya | 1,352.5 |
| 118 | Minigranja 0003 - San Pedro | San Pedro | 1,366.2 |
| 130 | Minigranja 0004 - Gandalf | Las Pitallas | 1,315.6 |
| 108 | Minigranja 0005 - Canahuate | Las Pitillas | 1,315.6 |
| 144 | Minigranja 0006 - Perija | Las Pitillas | 1,312.4 |
| 127 | Minigranja 0007 - La Paz Vallenata | La Paz | 1,329.7 |
| 113 | Minigranja 0008 - La Paz Verso | La Paz | 1,338.6 |
| 143 | Minigranja 0009 - El Molino | El Molino | 1,322.4 |
| 149 | Minigranja 0010 - Villanueva | Villanueva | 1,338.6 |
| 104 | Minigranja 0011 - El Roble | Cayo de Palma | 1,338.6 |
| 150 | Minigranja 0012 - La Reserva | Sabana de Torres | 1,339.6 |
| 157 | Minigranja 0013 - La Mesa | Los Santos | 1,339.6 |
| 153 | Minigranja 0014 - El Olimpo | Los Santos | 1,339.6 |
| 146 | Minigranja 0015 - El Son | Valledupar | 1,339.6 |
| 148 | Minigranja 0016 - La Puya | Valledupar | 1,304.3 |
| 147 | Minigranja 0017 - La Paz Esmeralda | La Paz | 1,339.6 |
| 102 | Minigranja 0018 - La Paz Leyenda | La Paz | 1,339.6 |
| 145 | Minigranja 0019 - El Merengue | Valledupar | 1,317.6 |
| 154 | Minigranja 0021 - Ibirico | La Jagua de Ibirico | 1,320.8 |
| 160 | Minigranja 0022 - La Cumbia | Valledupar | 1,320.8 |
| 156 | Minigranja 0023 - Joropo | Valledupar | 1,320.8 |
| 159 | Minigranja 0024 - San Diego Sur | San Diego | 1,320.8 |
| 168 | Minigranja 0025 - El Copey | El Copey | 1,320.8 |
| 162 | Minigranja 0026 - Valencia Or_1 | Valledupar | 1,320.8 |
| 161 | Minigranja 0027 - Valencia Or_2 | Valledupar | 1,320.8 |
| 167 | Minigranja 0028 - Chiriguana N1 | Chiriguana | 1,350.5 |
| 175 | Minigranja 0032 - EL Paso Norte | El Paso | 1,320.8 |
| 165 | Minigranja 0040 - La Cacica | Valledupar | 1,320.8 |
| 166 | Minigranja 0041 - Las Piloneras | Valledupar | 1,320.8 |
| 174 | Minigranja 0075 - Chiriguana N2 | Chiriguana | 1,320.8 |
| 173 | Minigranja 0077 - Chiriguana N4 | Chiriguana | 1,320.8 |
| 111 | Cedillanos | Yarumal | 1,251.2 |
| 164 | Ladrillera Del Meta | Acacias | 1,622.4 |
| 158 | Nestle DPA | Valledupar | 2,095.6 |

**39 Non-minifarms (self-consumption/GD):** IML Empaques (981kWp), El Encanto (1,349kWp), IML Etiquetas (340kWp), Clinica Somer (272kWp), Salud Vegas (123kWp), Pola del Pub (94kWp), and 33 more.

---

## 3. Unergy API (Operations Platform)

**URL:** `https://api.unergy.io`
**Auth:** Django session auth via admin portal
**Account:** `XFtY7e`
**Login:** `${UNERGY_API_USER}` / `${UNERGY_API_PASSWORD}` (env vars)
**Admin URL:** `https://api.unergy.io/XFtY7e/`
**Health check:** `GET /health/` -> `{"status": "ok"}`

### Authentication

Login is via the Django admin interface at `/{account}/login/` using CSRF token + session cookie. After login, redirects to `/accounts/profile/`.

```python
# 1. GET /{account}/login/ to obtain CSRF token (from cookie)
# 2. POST /{account}/login/ with form data:
#    username=${UNERGY_API_USER}, password=${UNERGY_API_PASSWORD}, csrfmiddlewaretoken=<csrf>
# 3. Session cookie provides auth for subsequent requests
```

**Note:** The OpenAPI schema at `GET /schema/` returns empty `paths: {}` -- all API endpoints are behind authentication and not publicly documented.

### OpenAPI Schema Info

```yaml
openapi: 3.0.3
info:
  title: API Operations
  version: v1
  description: "Some endpoints to make more operations tasks"
  contact:
    name: Sebastian
    email: sebastian@unergy.io
  license:
    name: BSD License
tags:
  - name: "2. XM Data Collection"
    description: "Consulta de datos de XM e IPP"
  - name: "3. XM Invoice Processing"
    description: "Procesamiento de facturas XM"
  - name: "4. Calculations & Settlements"
    description: "Calculos y liquidaciones"
  - name: "5. Reports & Documents"
    description: "Reportes principales"
  - name: "6. Auxiliary Documents"
    description: "Documentos auxiliares"
```

### Django Admin Models (Discovered)

The admin dashboard reveals these registered Django apps and models:

#### `unergy_model` (Core Business)

| Model | Admin URL | Description |
|-------|-----------|-------------|
| `project` | `/XFtY7e/unergy_model/project/` | Solar projects/plants |
| `subproject` | `/XFtY7e/unergy_model/subproject/` | Sub-projects |
| `meter` | `/XFtY7e/unergy_model/meter/` | Energy meters (linked to Quoia via `quoia_node_id`) |
| `company` | `/XFtY7e/unergy_model/company/` | Companies (investors, EPCs) |
| `companyinvoice` | `/XFtY7e/unergy_model/companyinvoice/` | Company invoices |
| `companyinvoiceperproject` | `/XFtY7e/unergy_model/companyinvoiceperproject/` | Invoice breakdown per project |
| `contractenergy` | `/XFtY7e/unergy_model/contractenergy/` | Energy contracts |
| `contractenergyproject` | `/XFtY7e/unergy_model/contractenergyproject/` | Contract-project assignments |
| `energycontractquantity` | `/XFtY7e/unergy_model/energycontractquantity/` | Contract energy quantities |
| `participant` | `/XFtY7e/unergy_model/participant/` | Investors/participants |
| `participantprojectagreement` | `/XFtY7e/unergy_model/participantprojectagreement/` | Participation agreements |
| `projectgeneration` | `/XFtY7e/unergy_model/projectgeneration/` | Monthly generation data |
| `projectdailyenergy` | `/XFtY7e/unergy_model/projectdailyenergy/` | Daily energy data |
| `medicionelectrica` | `/XFtY7e/unergy_model/medicionelectrica/` | Electrical measurements |
| `xminvoice` | `/XFtY7e/unergy_model/xminvoice/` | XM market invoices (Facturas XM) |
| `xminvoicefield` | `/XFtY7e/unergy_model/xminvoicefield/` | XM invoice line items |
| `distributionxm` | `/XFtY7e/unergy_model/distributionxm/` | XM energy distribution |
| `distributionxmperproject` | `/XFtY7e/unergy_model/distributionxmperproject/` | Distribution per project |
| `marketsettlement` | `/XFtY7e/unergy_model/marketsettlement/` | Market settlements |
| `monthlyipp` | `/XFtY7e/unergy_model/monthlyipp/` | Monthly IPP (Producer Price Index) |
| `dispcontractsftpxm` | `/XFtY7e/unergy_model/dispcontractsftpxm/` | XM FTP contract dispatch |
| `investormonthlysettlement` | `/XFtY7e/unergy_model/investormonthlysettlement/` | Investor monthly settlements |
| `maintenance` | `/XFtY7e/unergy_model/maintenance/` | Maintenance records |
| `maintenancefile` | `/XFtY7e/unergy_model/maintenancefile/` | Maintenance files |
| `maintenancebalance` | `/XFtY7e/unergy_model/maintenancebalance/` | Maintenance cost tracking |
| `revenueandcost` | `/XFtY7e/unergy_model/revenueandcost/` | Revenue and cost entries |
| `revenueandcosttype` | `/XFtY7e/unergy_model/revenueandcosttype/` | Rev/cost categories |
| `pricecolombianstock` | `/XFtY7e/unergy_model/pricecolombianstock/` | Colombian energy market prices |
| `shortagepricecolombianstock` | `/XFtY7e/unergy_model/shortagepricecolombianstock/` | Shortage prices |
| `unergytariff` | `/XFtY7e/unergy_model/unergytariff/` | Unergy tariff rates |
| `cityweatherdata` | `/XFtY7e/unergy_model/cityweatherdata/` | Weather data per city |
| `projectlocation` | `/XFtY7e/unergy_model/projectlocation/` | Project locations |
| `projectcompany` | `/XFtY7e/unergy_model/projectcompany/` | Project-company assignments |
| `projectinstallercompany` | `/XFtY7e/unergy_model/projectinstallercompany/` | Installer company per project |
| `projectpercentageadmin` | `/XFtY7e/unergy_model/projectpercentageadmin/` | Admin fee percentages |
| `userconfiguration` | `/XFtY7e/unergy_model/userconfiguration/` | User settings |
| `userinstance` | `/XFtY7e/unergy_model/userinstance/` | User instances |
| `crossinvoiceexcel` | `/XFtY7e/unergy_model/crossinvoiceexcel/` | Cross-invoice Excel exports |
| `incomestatmentexcel` | `/XFtY7e/unergy_model/incomestatmentexcel/` | Income statement exports |

#### `pwatt` (Financial Models)

| Model | Description |
|-------|-------------|
| `energyprice` | Energy price schedules |
| `projectfinancialmodel` | Project financial models |

#### `odoo` (ERP Integration)

| Model | Description |
|-------|-------------|
| `projectinvoicesetup` | Odoo invoice setup per project |

#### `weather_monitor`

| Model | Description |
|-------|-------------|
| `currentweather` | Current weather data |

#### `landing`

| Model | Description |
|-------|-------------|
| `city` | Landing page city data |

### Project Model Fields (from admin form)

| Field | Type | Description |
|-------|------|-------------|
| `nombre_topico` | text | Topic name (MQTT?) |
| `nombre_proyecto` | text | Project name |
| `nombre_corto` | text | Short name |
| `project_type` | choice | `0`=Self Consumption, `1`=Donation, `2`=Solar Farm |
| `gmt_zone` | text | Timezone |
| `tiempo_op_compra` / `valor_op_compra` | numeric | Purchase operation time/value |
| `capex_total_amount` / `capex_tax_amount` | numeric | Capital expenditure |
| `num_paneles` | integer | Panel count |
| `potencia_instalada_kwp` | numeric | Installed capacity (kWp) |
| `ac_power` | numeric | AC power |
| `produccion_especifica` | numeric | Specific production |
| `fecha_lanzamiento` | date | Launch date |
| `fecha_inicio_instalacion` | date | Installation start |
| `fecha_entrada_operacion` | date | COD (commercial operation date) |
| `fecha_inicio_rentabilidad` | date | Profitability start |
| `fecha_legalizacion` | date | Legalization date |
| `estimated_profit_rate` / `_min` / `_max` | numeric | Profit rate estimates |
| `estimated_irr` | numeric | Internal rate of return |
| `project_symbol` | text | Trading symbol |
| `currency` | text | Currency |
| `stable_contract` / `uwatt_contract` / `pwatt_contract` | text | Contract references |
| `estado_proyecto` | choice | Caracterizacion / Cotizacion / Proximamente / Financiacion / Instalacion / **Produccion** / Finalizado / Cancelado |
| `estado_financiacion` | choice | Cerrado / Abierto / Subasta / Completo |
| `tariff_indexing_type` | choice | `ipc` (CPI) / `ipp` (PPI) / `flat` |
| `num_total_acciones` | integer | Total shares |
| `costo_accion` | numeric | Share price |
| `from_generator` / `from_commercializer` | boolean | Revenue source flags |
| Various `_enabled` booleans | boolean | Feature flags (marketplace, invoicing, profit distribution, bonus) |

### Meter Model Fields

| Field | Type | Description |
|-------|------|-------------|
| `id_medidor` | text | Meter identifier |
| `meter_type` | choice | Meter type |
| `project` | FK | -> Project |
| `sub_project` | FK | -> SubProject |
| `border` | text | Frontier border point |
| `active` | boolean | Active flag |
| `priority` | integer | Priority |
| `measurement_type` | choice | Measurement type |
| `frame` / `frame_encoding` | text | Data frame format |
| `device_topic` | text | MQTT topic |
| `connection_type` | choice | Connection type |
| `ip_vpn` | text | VPN IP address |
| `public_key` | text | Public key |
| **`quoia_node_id`** | integer | **Link to Quoia meter** |
| `serial_number` | text | Serial number |
| `scale_factor` | numeric | Scale factor |
| `current_transformer_orientation` | choice | CT orientation |
| `iaep1/2/3_offset` | numeric | Import active energy phase offsets |
| `eaep1/2/3_offset` | numeric | Export active energy phase offsets |
| `irep1/2/3_offset` | numeric | Import reactive energy phase offsets |
| `erep1/2/3_offset` | numeric | Export reactive energy phase offsets |

### Contract Energy Fields

| Field | Type | Description |
|-------|------|-------------|
| `date_from` / `date_to` | date | Contract period |
| `code` | text | Contract code |
| `contract_type` | choice | Contract type |
| `tariff_price_type` | choice | Tariff pricing model |
| `percentage` | numeric | Percentage |
| `company` | FK | -> Company |
| `date_of_creation` | date | Creation date |

### Company Model Fields

| Field | Type | Description |
|-------|------|-------------|
| `person_type` / `company_type` | choice | Entity classification |
| `nombre` / `apellido` | text | Person name |
| `nombre_empresa` | text | Company name |
| `nit` | text | Colombian tax ID |
| `phone` / `email` | text | Contact |
| `id_type` / `id_number` | text | ID document |
| Various PEP/tax flags | boolean | Regulatory compliance |
| `vat_system` | choice | VAT regime |
| `odoo_partner_id` | integer | Odoo ERP link |
| `energy_cost` | numeric | Energy cost |
| `show_microsite` | boolean | Public microsite flag |
| `send_weekly_report` | boolean | Weekly report flag |

---

## Cross-API Data Flow

```
Quoia (Smart Meters)                    Solenium (Inverters)
  meter.id (=300)                         project.id (=74)
       |                                       |
       | quoia_node_id                         | plant_code (NE=...)
       v                                       v
Unergy API: unergy_model.meter --------> unergy_model.project
                                              |
                                              | project_id
                                              v
                                    OriginabotDB: projects
                                              |
                                              | project_id
                                              v
                                    RequestsDB: supplies_supplyrequest
```

**Key links:**
- `Unergy meter.quoia_node_id` -> `Quoia meter.id` (frontera data)
- `Solenium project.plant_code` -> `Unergy project.nombre_topico` (inverter data)
- `Unergy project.id` -> `OriginabotDB projects.id` (design data)
- `OriginabotDB projects.id` -> `RequestsDB supplyrequest.project` (grid connection)

---

## Rate Limits & Operational Notes

### Quoia
- No explicit rate limits discovered
- Pagination: 20 items per page (configurable via `page_size`)
- Token does not appear to expire

### Solenium
- JWT access token: ~5 min lifetime (refresh available)
- Paginated responses (default 20/page for project_summary)
- Some endpoints have server-side bugs (e.g., `quoia_measurements` returns 500 for some projects)
- All 35 projects currently show `disconnect` availability -- likely a data pipeline issue

### Unergy API
- No API endpoints documented in OpenAPI schema (paths empty)
- All data access via Django admin session
- Celery Beat handles periodic tasks
- Tags suggest XM data collection, invoice processing, settlements, and reports are the core operations
- Contact: Sebastian (sebastian@unergy.io)
