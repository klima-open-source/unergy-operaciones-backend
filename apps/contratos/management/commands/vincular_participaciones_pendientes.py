"""Vincula a su participación los contratos de representación que quedaron sin ella.

**Por defecto solo lee.** Escribe únicamente con `--aplicar`.

    python manage.py vincular_participaciones_pendientes
    python manage.py vincular_participaciones_pendientes --aplicar

Para qué. `registrar_proyectos_contratos` (paso 4a) dejó 33 contratos de
representación sin participación: el inversionista escrito en el contrato no era
participante de la planta. 20 eran de Ayura en plantas cargadas a nombre del
patrimonio. Después de `cargar_patrimonios`, Ayura y Solenium SÍ son participantes,
y este comando completa el vínculo.

Lee el inversionista escrito en `contratos.inversionista_id` con SQL directo: la
columna ya no es del modelo desde `contratos/0018` y sigue en la base hasta
`contratos/0023` (subida de los borrados). Hay que correrlo antes; después, dice que
la columna ya no existe en vez de fallar.

Solo toca filas de `contrato_proyectos` sin participación, y solo si la planta
tiene exactamente una participación de ese cliente. Si el contrato no tiene la
ficha sino solo el nombre escrito a mano, se compara normalizado (sin puntos ni
mayúsculas: "Ayura S.A.S." = "AYURA SAS") con los participantes de la planta. Lo que no se resuelve sale
listado con su motivo. Idempotente.
"""
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from apps.contratos.services.representacion_dedup import norm

PENDIENTES = """
SELECT cp.id, k.id, k.numero_contrato, p.nombre_comercial, cp.proyecto_id,
       k.inversionista_id, coalesce(cl.razon_social_nombre, trim(k.inversionista_nombre)),
       trim(k.inversionista_nombre)
  FROM contrato_proyectos cp
  JOIN contratos k ON k.id = cp.contrato_id
  JOIN proyectos p ON p.id = cp.proyecto_id
  LEFT JOIN clientes cl ON cl.id = k.inversionista_id
 WHERE k.grupo = 'representacion_cgm' AND k.deleted_at IS NULL
   AND cp.proyecto_inversionista_id IS NULL
 ORDER BY p.nombre_comercial, k.id
"""

PARTICIPACIONES = """
SELECT pi.id FROM proyecto_inversionistas pi
  JOIN inversionistas i ON i.id = pi.inversionista_id
 WHERE pi.proyecto_id = %s AND i.cliente_id = %s
"""

PARTICIPANTES = """
SELECT pi.id, cl.razon_social_nombre FROM proyecto_inversionistas pi
  JOIN inversionistas i ON i.id = pi.inversionista_id
  JOIN clientes cl ON cl.id = i.cliente_id
 WHERE pi.proyecto_id = %s
"""


class Command(BaseCommand):
    help = "Vincula contratos de representación sin participación (solo lee sin --aplicar)."

    def add_arguments(self, parser):
        parser.add_argument("--aplicar", action="store_true", help="Escribe los cambios.")

    def handle(self, *args, aplicar=False, **opciones):
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'contratos' AND column_name = 'inversionista_id'"
            )
            if not cur.fetchone()[0]:
                self.stdout.write("La columna contratos.inversionista_id ya no existe "
                                  "(contratos/0023): no hay de dónde leer. Nada que hacer.")
                return
            cur.execute(PENDIENTES)
            pendientes = cur.fetchall()
            vinculados, sin_resolver = 0, []
            for (cp_id, contrato_id, numero, planta, proyecto_id, cliente_id, nombre,
                 escrito) in pendientes:
                if cliente_id is None and not escrito:
                    motivo = "el contrato no dice inversionista"
                else:
                    if cliente_id is not None:
                        cur.execute(PARTICIPACIONES, [proyecto_id, cliente_id])
                        ids = [fila[0] for fila in cur.fetchall()]
                    else:
                        cur.execute(PARTICIPANTES, [proyecto_id])
                        ids = [pi_id for pi_id, razon in cur.fetchall()
                               if norm(razon) == norm(escrito)]
                    if len(ids) == 1:
                        cur.execute("UPDATE contrato_proyectos SET proyecto_inversionista_id = %s "
                                    "WHERE id = %s", [ids[0], cp_id])
                        vinculados += 1
                        self.stdout.write(f"  vinculado: #{contrato_id} {numero or ''} | {planta} "
                                          f"| {nombre} → participación #{ids[0]}")
                        continue
                    motivo = ("el inversionista no participa en la planta" if not ids
                              else "el inversionista tiene varias participaciones")
                sin_resolver.append((contrato_id, numero, planta, nombre, motivo))
            if not aplicar:
                transaction.set_rollback(True)

        self.stdout.write(f"\nSin participación: {len(pendientes)}. Vinculados: {vinculados}. "
                          f"Sin resolver: {len(sin_resolver)}.")
        for contrato_id, numero, planta, nombre, motivo in sin_resolver:
            self.stdout.write(f"  - #{contrato_id} {numero or ''} | {planta} | {nombre or '—'} "
                              f"| {motivo}")
        if not aplicar:
            self.stdout.write("Solo lectura: corre con --aplicar para escribir.")
