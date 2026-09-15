"""Revisa los proyectos donde `codigo_tsf` no tiene la forma que debería.

**Por defecto solo reporta. Con `--ejecutar` escribe.**

    docker compose exec operaciones python manage.py revisar_codigo_tsf
    docker compose exec operaciones python manage.py revisar_codigo_tsf --ejecutar

Qué busca. Sun Factory manda un solo dato, el `base_name`, con la forma
`COLCEST924P3_ASTREA_ORIENTE`. De ahí salen dos campos nuestros:

    origina_code = COLCEST924P3_ASTREA_ORIENTE   (el base_name entero)
    codigo_tsf   = COLCEST924P3                  (solo el prefijo CREG)

Pero `codigo_tsf` es también un campo del formulario: una persona lo escribe al
crear un proyecto a mano, y ahí nadie valida el formato. Si pega el código
completo, queda el `base_name` en el campo del prefijo y `origina_code` vacío.

Eso rompe el emparejamiento de `tsf_sync`, que cruza por el prefijo para
reconocer un proyecto que ya existe -- de hecho hay una línea que también prueba
`Q(codigo_tsf=base_name)`, un parche para justo este caso.

Medido el 2026-09-15: 3 de 117 proyectos con código, las tres Astrea (Calipso,
Rigel y Titán), todas sin `origina_code`. Casi seguro creadas a mano. Astrea 1
fue además el duplicado que motivó la restricción única de `codigo_tsf`.

Qué hace con `--ejecutar`: mueve el valor a `origina_code` y deja el prefijo en
`codigo_tsf`. **Solo si `origina_code` está vacío** -- si ya tiene algo, no se
pisa: puede ser distinto y decidir cuál vale no es cosa de un comando.
"""
import re

from django.core.management.base import BaseCommand

from apps.proyectos.models import Proyecto

# Un código CREG: COL + letras y números, sin separadores.
PREFIJO_CREG = re.compile(r"^COL[A-Z0-9]+$")


class Command(BaseCommand):
    help = "Reporta (y opcionalmente corrige) los `codigo_tsf` mal formados."

    def add_arguments(self, parser):
        parser.add_argument(
            "--ejecutar", action="store_true",
            help="Escribe los cambios. Sin esto solo reporta.",
        )

    def handle(self, *args, **opciones):
        con_codigo = (
            Proyecto.objects.filter(deleted_at__isnull=True)
            .exclude(codigo_tsf__isnull=True).exclude(codigo_tsf="")
            .order_by("nombre_comercial")
        )

        sospechosos = []
        raros = []
        for p in con_codigo.only("id", "nombre_comercial", "codigo_tsf", "origina_code"):
            codigo = (p.codigo_tsf or "").strip()
            if PREFIJO_CREG.match(codigo):
                continue
            prefijo = codigo.split("_", 1)[0]
            if PREFIJO_CREG.match(prefijo):
                sospechosos.append((p, prefijo))
            else:
                # Ni prefijo válido ni base_name reconocible: no se toca.
                raros.append(p)

        self.stdout.write(f"Proyectos con código TSF: {con_codigo.count()}")
        self.stdout.write(f"  Con el base_name completo: {len(sospechosos)}")
        self.stdout.write(f"  Con un formato que no reconozco: {len(raros)}\n")

        if raros:
            self.stdout.write(self.style.NOTICE(
                "FORMATO NO RECONOCIDO -- se dejan intactos, hay que mirarlos a mano:"
            ))
            for p in raros:
                self.stdout.write(f"  #{p.id} {p.nombre_comercial}: {p.codigo_tsf!r}")
            self.stdout.write("")

        if not sospechosos:
            self.stdout.write(self.style.SUCCESS("Nada que corregir."))
            return

        self.stdout.write(self.style.WARNING("EL BASE_NAME QUEDÓ EN EL CAMPO DEL PREFIJO:"))
        a_guardar = []
        bloqueados = []
        for p, prefijo in sospechosos:
            actual_origina = (p.origina_code or "").strip()
            if actual_origina and actual_origina != p.codigo_tsf:
                bloqueados.append((p, prefijo))
                continue
            self.stdout.write(
                f"  #{p.id} {p.nombre_comercial}\n"
                f"       codigo_tsf   {p.codigo_tsf!r} -> {prefijo!r}\n"
                f"       origina_code {actual_origina or '(vacío)'!r} -> {p.codigo_tsf!r}"
            )
            p.origina_code = p.codigo_tsf
            p.codigo_tsf = prefijo
            a_guardar.append(p)

        if bloqueados:
            self.stdout.write(self.style.ERROR(
                "\nCON `origina_code` YA OCUPADO -- no se tocan: hay dos valores "
                "distintos y elegir cuál vale no es cosa de un comando."
            ))
            for p, prefijo in bloqueados:
                self.stdout.write(
                    f"  #{p.id} {p.nombre_comercial}: "
                    f"codigo_tsf={p.codigo_tsf!r} origina_code={p.origina_code!r}"
                )

        if not opciones["ejecutar"]:
            self.stdout.write(self.style.WARNING(
                f"\nNada escrito. Con --ejecutar se corrigen {len(a_guardar)}."
            ))
            return

        Proyecto.objects.bulk_update(a_guardar, ["codigo_tsf", "origina_code"])
        self.stdout.write(self.style.SUCCESS(f"\n{len(a_guardar)} proyecto(s) corregido(s)."))
