# API de portafolios

Un portafolio es una agrupación de plantas; la usan los informes «Por portafolio».
**Un proyecto está en un solo portafolio**: `proyectos.portafolio_id` (Sara,
2026-10-15, después de probar varios por planta con la tabla `portafolio_proyectos`,
que queda fuera del modelo y se borra en un despliegue posterior).

## Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/api/v1/portafolios` | Cada portafolio con sus plantas, y el pool de plantas operativas sin portafolio |
| `POST` | `/api/v1/portafolios[?forzar=true]` | Crea uno → `201`. Nombre parecido a otro → `409` con el candidato |
| `PATCH` | `/api/v1/portafolios/{id}[?forzar=true]` | Renombra |
| `DELETE` | `/api/v1/portafolios/{id}` | `204`. Sus plantas quedan sin portafolio |
| `PATCH` | `/api/v1/portafolios/asignar` | Pone una planta en un portafolio (o en ninguno) |

`GET`:

```json
{
  "portafolios": [
    { "id": 4, "nombre": "Ayura",
      "proyectos": [{ "id": 22, "nombre": "MGS 0004 Valle de Gandalf",
                      "sub_project": "...", "municipio": "..." }] }
  ],
  "sin_portafolio": [{ "id": 31, "nombre": "...", "sub_project": "...", "municipio": "..." }]
}
```

`asignar`:

```json
{ "proyecto_id": 22, "portafolio_id": 4 }
```

La pone en ese portafolio y la saca del que tenía; `portafolio_id: null` la deja
sin portafolio. Responde `{ "ok": true, "proyecto_id": 22, "portafolio_id": 4 }`.
Con `en_portafolio: false` (el cuerpo del front de varios portafolios) la saca de
`portafolio_id` solo si es el suyo.

## Dónde más aparece

- `GET /api/v1/proyectos/{id}`: `portafolio_id` (escribible) y `portafolio_nombre`.
  `GET /api/v1/proyectos?portafolio_id=4` filtra.
- Asistente de informes (`/monitoreo/...portfolios`): `{nombre: [plantas]}`, solo
  plantas operativas.
- Cumplimiento PPA (`vista-contratos`): el portafolio dominante entre sus plantas.
- Fusión de proyectos: si el ganador no tiene portafolio, hereda el del perdedor.

## Datos

`volver_a_portafolio_unico` (solo lee sin `--aplicar`) copia `portafolio_proyectos`
a `proyectos.portafolio_id`. Si una planta quedó en varios portafolios no la toca y
la lista, para elegir uno a mano.
