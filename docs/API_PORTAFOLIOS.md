# API de portafolios

Un portafolio es una agrupación de plantas; la usan los informes «Por portafolio».
**Una planta puede estar en varios portafolios** (modelo de Sara, 2026-10-15): la
pertenencia es una fila de `portafolio_proyectos (portafolio_id, proyecto_id)`,
única por pareja.

## Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/api/v1/portafolios` | Cada portafolio con sus plantas, y el pool de plantas operativas sin ninguno |
| `POST` | `/api/v1/portafolios[?forzar=true]` | Crea uno → `201`. Nombre parecido a otro → `409` con el candidato |
| `PATCH` | `/api/v1/portafolios/{id}[?forzar=true]` | Renombra |
| `DELETE` | `/api/v1/portafolios/{id}` | `204`. Las plantas no se borran, solo sus pertenencias a ese portafolio |
| `PATCH` | `/api/v1/portafolios/asignar` | Mete o saca una planta de un portafolio |

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

Una planta en dos portafolios aparece en los dos. Ya no hay `activo`.

`asignar`:

```json
{ "proyecto_id": 22, "portafolio_id": 4, "en_portafolio": true }
```

`true` la mete (idempotente), `false` la saca de ese portafolio y deja los demás.
Responde `{ "ok": true, "proyecto_id": 22, "portafolio_ids": [4, 7] }`.

Sin `en_portafolio` es el cuerpo del frontend anterior: **mueve** la planta a
`portafolio_id` (la deja solo ahí; `null` la saca de todos).

## Dónde más aparece

- `GET /api/v1/proyectos/{id}`: `portafolios: [{id, nombre}]`. `GET /api/v1/proyectos?portafolio_id=4`
  filtra por pertenencia.
- Asistente de informes (`/monitoreo/...portfolios`): `{nombre: [plantas]}`, solo plantas
  operativas; una planta sale en todos sus portafolios.
- Cumplimiento PPA (`vista-contratos`): el portafolio dominante entre todos los de sus plantas.

## Datos

`copiar_portafolios` (solo lee sin `--aplicar`) copia la columna vieja
`proyectos.portafolio_id` a la tabla, e imprime en CSV el texto suelto
`contratos.portafolio` antes de su borrado (`proyectos/0018`, `contratos/0028`).
