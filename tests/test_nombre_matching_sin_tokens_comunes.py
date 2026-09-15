"""Dos nombres sin una palabra en común no son "parecidos".

Bug reportado el 2026-09-15: al agregar la frontera **"GD Caracolí 2"**, el
aviso decía que ya existía una parecida: **"La Catedral"**. No se parecen en
nada.

El algoritmo combina tres señales --solapamiento de tokens, cobertura y
similitud de TEXTO-- y se queda con la mayor. La tercera está para tolerar
erratas ("Caracoli" contra "Caracolli"), pero podía sostener un match ella sola:

    tokens("GD Caracolí 2") = {caracoli, 2}     ("gd" es stopword)
    tokens("La Catedral")   = {catedral}        ("la" es stopword)
    en común                = {}                  ← ninguna
    ratio("2 caracoli", "catedral") = 0.556       ← y el umbral es 0.55

Pasaba por 6 milésimas, sin compartir una sola palabra, solo porque las letras
se parecen.

Ahora, sin tokens en común, el texto tiene que parecerse mucho más (0.80) para
contar. Una errata de verdad --una letra de más en una palabra-- supera 0.9; el
parecido accidental ronda 0.5.

Esto lo usan TRES dominios --fronteras, proyectos y operadores de red-- así que
el falso positivo aparecía en los tres.
"""
import pytest

django = pytest.importorskip("django", reason="requiere el entorno de Django (uv sync)")


@pytest.fixture(scope="module", autouse=True)
def _base():
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    os.environ.setdefault("SECRET_KEY", "x" * 40)
    django.setup()


def _score(a, b):
    from apps.comun.nombre_matching import score_nombre

    return score_nombre(a, [b])


def _acepta(a, b):
    from apps.comun.nombre_matching import mejor_candidato

    item, _ = mejor_candidato(a, [(b, [b])])
    return item is not None


# ── El caso del bug ─────────────────────────────────────────────────────────


def test_caracoli_no_se_parece_a_la_catedral():
    assert not _acepta("GD Caracolí 2", "La Catedral")


@pytest.mark.parametrize("a,b", [
    ("GD Caracolí 2", "La Catedral"),
    ("Bayunca", "Cañahuate"),
    ("El Son", "Los Coches"),
    ("Perijá", "Piojo Sur"),
])
def test_nombres_distintos_sin_palabras_en_comun_no_avisan(a, b):
    assert not _acepta(a, b), f"{a!r} y {b!r} no comparten ninguna palabra"


# ── Lo que TIENE que seguir funcionando ─────────────────────────────────────


def test_una_errata_sigue_detectandose():
    """Para esto existe la similitud de texto: una letra de más."""
    assert _acepta("Caracoli", "Caracolli")


def test_el_ejemplo_que_documenta_el_modulo_sigue_andando():
    """"AGGE Extractora Monterrey" contra "AGGE Frontera Monterrey" -- el caso
    por el que se eligió un algoritmo difuso y no una comparación literal."""
    assert _acepta("AGGE Extractora Monterrey", "AGGE Frontera Monterrey")


def test_un_nombre_contenido_en_otro_sigue_avisando():
    assert _acepta("GD Caracolí 2", "GD Caracolí")


def test_compartir_una_palabra_alcanza():
    """Con token en común, el umbral normal sigue mandando: no se endureció el
    caso general, solo el que no comparte nada."""
    assert _acepta("Minigranja Perijá", "Perijá Norte")


# ── Lo que ya estaba protegido y no se toca ─────────────────────────────────


def test_dos_pueblos_distintos_siguen_sin_confundirse():
    """"san"/"santa" son stopwords porque casi todo municipio del portafolio
    empieza así."""
    assert not _acepta("GD San Pelayo", "GD San Marcos")


def test_el_numero_de_minigranja_sigue_decidiendo():
    """Números distintos: son minigranjas distintas por más que compartan el
    municipio."""
    assert not _acepta(
        "Minigranja 0091 - San Luis de Sincé", "Minigranja 0088 - San Luis"
    )


def test_el_mismo_numero_sigue_ganando():
    assert _acepta("Minigranja 0091 - San Luis de Sincé", "MGS 0091 Since Occidente")


# ── El umbral ───────────────────────────────────────────────────────────────


def test_el_piso_sin_tokens_comunes_es_mas_alto_que_el_normal():
    from apps.comun.nombre_matching import UMBRAL_ACEPTAR, UMBRAL_SIN_TOKENS_COMUNES

    assert UMBRAL_SIN_TOKENS_COMUNES > UMBRAL_ACEPTAR


def test_sin_tokens_comunes_el_score_cae_a_cero():
    """No se devuelve el valor bajo: se anula. Si se devolviera, podría seguir
    ganándole a otro candidato peor y volver a elegirlo."""
    assert _score("GD Caracolí 2", "La Catedral") == 0.0
