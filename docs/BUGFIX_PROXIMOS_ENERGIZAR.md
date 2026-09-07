# Bugfix: "Proyectos próximos a energizarse" — RESUELTO 2026-06-09

Vista `/operaciones` (Cumplimiento) → "Proyectos próximos a energizarse".
Endpoint backend: `GET /api/v1/proximos-energizar` (`app/api/v1/proximos_energizar.py`).

Fuente del pipeline: `originabotdb` (`minifarm_project` + `minifarm_projectstagechange`),
cruzado con cronogramas Sun Factory (fecha de energización + % avance) y la API de
generación de Unergy (detección de plantas ya generando).

## Síntoma
La vista mostraba: *"No se pudo leer el pipeline desde originabotdb — revisar
conexión/credenciales o el esquema de minifarm_projectstagechange."* (`source=error`).

## Causa raíz — dos bugs encadenados

### Bug 1 — Esquema alucinado ✅
Un daemon nightwatch inventó la columna `c.date` en `minifarm_projectstagechange`
(PR #7, commit `91e4ef5`); esa columna no existe. El esquema real es
`id, previous_stage, current_stage, created_at, project_id, justification,
review_date, review_date_notified_at`.
**Fix: commit `da32f01`** → `c.date` → `c.created_at`. La query corregida devuelve
**60 proyectos** (deploy=18, construction=29, bt_and_contract=13).

### Bug 2 — Railway no alcanzaba el Cloud SQL ✅
Tras el fix de esquema, prod seguía en `source=error` por `ConnectionTimeout`: el
firewall del Cloud SQL viejo (`34.74.198.101`) solo autorizaba la red de Unergy
(`186.117.247.4`, por eso funcionaba en local) y bloqueaba la IP egress de Railway
(`152.55.180.240`).

**Resolución (Camilo, CTO):** en vez de whitelistear la IP de Railway, migró el host
de la base a una **IP nueva alcanzable: `34.24.192.147`** (misma instancia,
credenciales y bases `originabotdb`/`requestsdb`).

**Fix aplicado:** cambiar el host `34.74.198.101` → `34.24.192.147` en las env vars
`ORIGINA_DATABASE_URL` y `REQUESTSDB_DATABASE_URL` (Railway + `.env` local). La IP
nunca estuvo hardcodeada en código.

## Estado final
- ✅ Env vars de Railway y `.env` local apuntando a `34.24.192.147`.
- ✅ Verificado en producción: `source=originabotdb`, `count=60`.
- ✅ Debug temporal retirado (commit `7745345`): se quitó el parámetro `?debug=1`
  (`detail` + `egress_ip` vía ipify) que se había agregado para diagnosticar el
  timeout (commits `a753e64`, `511517a`).
- ✅ IP actualizada en docs de referencia (atlas, review, discovery schemas).

## Verificación (consola del navegador, app logueada)
```js
fetch('https://operaciones.unergy.io/api/v1/proximos-energizar',{headers:{Authorization:'Bearer'+String.fromCharCode(32)+localStorage.getItem('token')}}).then(r=>r.json()).then(d=>console.log('SOURCE='+d.source+' | COUNT='+d.count))
```
Esperado: `SOURCE=originabotdb | COUNT=60`.

## Lección
Validar cualquier query a `originabotdb` contra la BD real antes de mergear (no
confiar en esquemas auto-generados por daemons). La IP del Cloud SQL puede cambiar:
vive solo en env vars, no hardcodear.
