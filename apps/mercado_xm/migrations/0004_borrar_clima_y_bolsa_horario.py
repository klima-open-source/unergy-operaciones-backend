"""Borra las cuatro tablas de clima y el precio de bolsa por hora (Sara, 2026-10-16).

Las llenaba EVO para tres pantallas del front que se retiran el mismo día:
Clima & ENSO, Balance Energía y la Precio de Bolsa vieja (la reemplaza la
pestaña de bolsa de SIMEM). Ninguna otra parte del sistema las lee:

    clima_oni_monthly      ONI/SOI/PDO por mes        (cargada a mano, una vez)
    clima_precip_monthly   lluvia por mes y región    (cargada a mano, una vez)
    clima_price_monthly    precio mensual + fase ENSO (cargada a mano, una vez)
    clima_forecasts        pronóstico diario          (tarea de las 06:00, retirada)
    precios_bolsa_horario  precio de bolsa por hora   (la escribía `ingesta_bolsa`)

Ninguna tiene clave foránea hacia o desde otra tabla. Se pierden los datos: Sara
decidió no guardar copia. `precios_bolsa_diario` se queda (tablero,
Descubrimientos, Cumplimiento).

Nacieron fuera de Django (revisión 135 de Alembic), sin modelo; por eso RunSQL.
La vuelta atrás recrea la estructura vacía, no los datos.
"""
from django.db import migrations

TABLAS = [
    "clima_oni_monthly",
    "clima_precip_monthly",
    "clima_price_monthly",
    "clima_forecasts",
    "precios_bolsa_horario",
]

RECREAR = """
CREATE TABLE IF NOT EXISTS clima_oni_monthly (
    id BIGSERIAL PRIMARY KEY,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    oni_value REAL NOT NULL,
    soi_value REAL,
    pdo_value REAL,
    mjo_amplitude REAL,
    enso_phase VARCHAR(20),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(year, month)
);
CREATE TABLE IF NOT EXISTS clima_precip_monthly (
    id BIGSERIAL PRIMARY KEY,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    region VARCHAR(50) NOT NULL,
    precip_mm REAL NOT NULL,
    anomaly_pct REAL,
    climatology_mm REAL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(year, month, region)
);
CREATE TABLE IF NOT EXISTS clima_price_monthly (
    id BIGSERIAL PRIMARY KEY,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    price_cop_kwh REAL NOT NULL,
    enso_phase VARCHAR(20),
    precip_andina_mm REAL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(year, month)
);
CREATE TABLE IF NOT EXISTS clima_forecasts (
    id BIGSERIAL PRIMARY KEY,
    forecast_date DATE NOT NULL,
    forecast_json JSONB NOT NULL,
    model_version VARCHAR(50) DEFAULT 'v1_statistical',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS precios_bolsa_horario (
    id BIGSERIAL PRIMARY KEY,
    fecha DATE NOT NULL,
    hora INTEGER NOT NULL CHECK (hora BETWEEN 1 AND 24),
    precio_cop_kwh REAL NOT NULL,
    gen_hidro REAL,
    gen_termica REAL,
    gen_renovable REAL,
    gen_menor REAL,
    planta_marginal VARCHAR(100),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(fecha, hora)
);
CREATE INDEX IF NOT EXISTS ix_clima_oni_ym ON clima_oni_monthly (year, month);
CREATE INDEX IF NOT EXISTS ix_clima_precip_ym ON clima_precip_monthly (year, month, region);
CREATE INDEX IF NOT EXISTS ix_clima_price_ym ON clima_price_monthly (year, month);
CREATE INDEX IF NOT EXISTS ix_clima_forecasts_date ON clima_forecasts (forecast_date DESC);
CREATE INDEX IF NOT EXISTS ix_precios_bolsa_horario_fecha ON precios_bolsa_horario (fecha DESC, hora);
"""


class Migration(migrations.Migration):

    dependencies = [
        ('mercado_xm', '0003_porcentaje_despacho_4_decimales'),
    ]

    operations = [
        migrations.RunSQL(
            "".join(f"DROP TABLE IF EXISTS {t};" for t in TABLAS),
            reverse_sql=RECREAR,
        ),
    ]
