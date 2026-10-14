# API de patrimonios autónomos

Modelo de Sara (2026-10-14). Una planta se puede tener **a través de un patrimonio
autónomo** (una fiducia). El **dueño** de la participación es el inversionista
(Ayura, Solenium…); al **patrimonio** se le factura y se le liquida.

Jerarquía de dos niveles: un patrimonio **general** (la fiduciaria) no tiene
`padre_id`; un **subpatrimonio** (P.A. Valle de Gandalf, código 17434) apunta a su
general. Una planta puede tenerse por un subpatrimonio o directo por un general.

## Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/api/v1/patrimonios` | Los generales, con sus subpatrimonios y las plantas de cada uno |
| `POST` | `/api/v1/patrimonios` | Crea uno → `201` |
| `PATCH` | `/api/v1/patrimonios/{id}` | Edita nombre, código, general o ficha |
| `DELETE` | `/api/v1/patrimonios/{id}` | `204`; `409` si tiene plantas o subpatrimonios |

Cuerpo de `POST`/`PATCH`:

```json
{ "nombre": "P.A. Valle de Gandalf", "codigo": "17434", "padre_id": 1, "cliente_id": 33 }
```

- `padre_id: null` = es un general. Un subpatrimonio no puede colgar de otro
  subpatrimonio, ni un general con subpatrimonios pasar a ser uno (`409`).
- `codigo` es único (`409` si se repite); vacío en un general.
- `cliente_id` es su ficha en `clientes` (razón social, NIT). Opcional: un
  subpatrimonio sin ficha propia se factura a la de su general.

Respuesta de `GET` (un elemento por general):

```json
{
  "id": 1, "nombre": "Fiduciaria Bancolombia", "codigo": null, "padre_id": null,
  "cliente_id": 33, "cliente_nombre": "PATRIMONIOS AUTONOMOS FIDUCIARIA BANCOLOMBIA…",
  "cliente_nit": "830.054.539-2", "participaciones": [],
  "subpatrimonios": [
    { "id": 2, "nombre": "P.A. Valle de Gandalf", "codigo": "17434", "padre_id": 1,
      "cliente_id": null, "cliente_nombre": null, "cliente_nit": null,
      "participaciones": [
        { "id": 41, "proyecto_id": 22, "proyecto_nombre": "MGS 0004 Valle de Gandalf",
          "inversionista_nombre": "Ayura S.A.S", "porcentaje_participacion": "1.0000000" }
      ] }
  ]
}
```

## En la participación

`GET|POST /api/v1/proyectos/{id}/inversionistas` y `PATCH …/{inv_id}` llevan
`patrimonio_id` (opcional, escribible) y `patrimonio_nombre` (lectura).
`es_patrimonio_autonomo` ya no existe: estaba en `false` en todas las filas y
nadie la leía.

## A quién se le factura

`apps/proyectos/services/patrimonios.py::destinatario(participacion)`: la ficha
del patrimonio, o la de su general si no tiene; sin patrimonio, la del
inversionista.
