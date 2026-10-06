"""Clientes de servicios externos (HTTP, SMTP, IMAP) portados desde `app/`.

Se copian tal cual del original, cambiando solo sus imports: cuanto menos se
le toque a un cliente de una API externa, menos hay que volver a verificar
contra esa API. La lista de lo que falta portar es `PRESTADOS_DE_APP` en
`tests/test_solo_django.py`.
"""
