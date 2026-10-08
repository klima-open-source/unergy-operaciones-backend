# Revisión: decimales que el front guardó redondeados

**Estado al 2026-10-08:** el arreglo está commiteado en el front (rama `fix/numberfield-decimales`,
commit `46e9d202`) y **sin desplegar**. Hasta que salga, estas pantallas siguen redondeando.

## Qué pasó

El 2026-09-28 varias pantallas del front pasaron de PrimeVue al campo de número de la librería
nueva (`NumberField` de Reka UI). Ese campo, por defecto, **ajusta lo escrito a pasos de 1**: un
6,5 se guarda como 7, un 3 950,25 como 3 950. Nadie lo desactivó, así que desde ese día cada
decimal escrito en esas pantallas se guardó como entero. Se descubrió el 2026-10-08 probando en el
navegador la migración del asistente de contratos.

Un valor redondeado **no se distingue** de uno que de verdad era entero. Por eso la revisión
compara contra el documento de origen (la oferta, la factura, el reporte de XM, el RUT).

## Cómo encontrar los sospechosos

El comando `revisar_decimales_redondeados` (solo lee) lista, tabla por tabla, las filas
modificadas desde el 2026-09-28 cuyo valor en una de estas columnas es entero, agrupadas por
valor:

```bash
gh workflow run comando.yml -f comando="revisar_decimales_redondeados (solo lee)"
```

En local: `python manage.py revisar_decimales_redondeados --csv sospechosos.csv`.

Descarta los enteros obviamente legítimos (0; 100 % de FNCER; despacho de 1,00 = 100 %). Las
tablas sin fecha propia usan la de su padre, así que un contrato tocado por otra razón —el corte
del 2026-10-07, por ejemplo— trae sus tarifas a la lista aunque nadie las haya editado: ahí el
criterio es si el **valor** es sospechoso, no la fecha.

## Los 36 campos

Todos entraron al front el **2026-09-28**. "Desde" es la fecha del commit en `main`; el despliegue
pudo ser ese día o el siguiente.

### Se guardan en una columna (revisar)

| # | Pantalla | Tabla.columna | Commit front |
|---|---|---|---|
| 1-4 | Clientes › editar cliente › IVA %, Retención %, ReteIVA %, ReteICA % (el mismo formulario está en Comercial › oportunidad › cliente y en Servicios › nuevo cliente) | `clientes.iva_pct`, `retencion_pct`, `reteiva_pct`, `reteica_pct` | `1453824c` |
| 5-8 | Clientes › Servicios › tasa por servicio › IVA %, Retención %, ReteIVA %, ReteICA % | `cliente_tasa_servicio.*_pct` | `b63205a4` |
| 9 | Comercial › Firmar oferta › Tarifa base | `contratos.tarifa_base` (PPA creado al firmar) | `2b542bb7` |
| 10 | Comercial › Firmar oferta › precios por año | `ppa_tarifas.tarifa` (una fila por mes) y `contratos.tarifa_base` | `2b542bb7` |
| 11 | Comercial › oferta › nueva versión › precio por año | `oportunidad_oferta_version_precios.precio`; **al firmar se copia** a `ppa_tarifas` y `contratos.tarifa_base` | `2b542bb7` |
| 12 | Fallas › detalle › kWh perdidos estimados | `fallas.kwh_perdidos_estimado` | `3c13068e` |
| 13-14 | Fallas › formulario › inversores › Potencia kW (editar y agregar) | `proyecto_inversores.potencia_nominal_kw`; se copia a `falla_inversores.potencia_kw` | `3c13068e` |
| 16-27 | Garantías › Ajustes XM › Histórico › editar registro: PB, Restricciones, STN, TRM, PTB, Total UNGC, Total UNGG, Total a consignar, Disponible custodia, Congelado, Saldo, Total ajuste TXR | `garantias_ajustes.pb`, `restricciones`, `stn`, `trm`, `ptb`, `total_ungc`, `total_ungg`, `total_consignar`, `disponible_custodia`, `congelado`, `saldo`, `total_ajuste_txr` | `bbd1b67e` |
| 30 | Liquidaciones › editar resumen › Tasa de cambio | `liquidaciones.tasa_cambio` | `de3b5742` |
| 31 | GESCON › registrar modificación › % despacho | `asic_solicitudes.porcentaje_despacho` (fracción 0-1) | `116d2f6c` |
| 32-33 | GESCON › contrato ASIC › % FNCER y % despacho | `asic_solicitudes.porcentaje_fncer` (0-100), `porcentaje_despacho` | `116d2f6c` |
| 34 | Registros CND › detalle ASIC › Parámetros 9.3 (V máx/nom/mín, In eq, Icc pico/3F/2F/1F/estable, impedancia, frecuencias) | `registro_parametros_93.*` (12 columnas) | `82cd6b73` |
| 35 | Retos › métrica › Meta | `retos_metrica.meta` | `96a2238e` |
| 36 | Retos › semana › valor de cada métrica | `retos_valor_semanal.valor` | `96a2238e` |

### No van a una columna (revisar a mano si importa)

| # | Pantalla | Dónde terminó |
|---|---|---|
| 15 | Garantías › Ajustes › PB semana anterior | Solo en el navegador (localStorage). Es el denominador de la variación del PB: un PB anterior redondeado dejó mal la variación en el mensaje y en `garantias_ajustes.snapshot` |
| 28 | Garantías › Descuento de facturas › Valor total | Solo suma lo descontado; termina en `garantias_ajustes.snapshot` (`facturasDescontadas`, `disponibleNeto`, `disponibleAplicacion`) |
| 29 | Garantías › Semanal › Variación PB % | El texto del mensaje y `garantias_ajustes.snapshot.variacionPb` |

## Cosas que salieron al revisar (no son del redondeo)

- **% despacho de GESCON:** el front lo manda dividido por 100 con 4 decimales, pero la columna
  es `DecimalField(5,2)`: un 33,33 % llega como 0,3333 y se guarda como 0,33. Pierde precisión
  aunque el front ya no redondee.
- **ReteICA %:** el front acepta 4 decimales y la columna guarda 2.
- **Garantías:** el front redondea a 2 decimales a propósito al guardar; el defecto del campo los
  dejaba en 0.
- **Precios de oferta:** los de una versión aceptada se vuelven a copiar al firmar. Un precio
  redondeado en la versión pasa al PPA aunque al firmar no se toque el campo.

## Medición en la copia local (datos al 2026-10-01)

Con 4 días de exposición: 1 tarifa base de PPA, 142 filas de tarifas de PPA (11 valores distintos,
todos de contratos tocados el 2026-09-29), 2 valores de garantías (`congelado` = 431 548 727).
Nada en clientes, ofertas, fallas, liquidaciones, GESCON, CREG 093 ni Retos. **Producción tiene
una semana más**: correr el comando allá.
