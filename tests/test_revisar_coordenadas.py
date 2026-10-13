"""El diagnóstico de `revisar_coordenadas` (función pura: no toca la base)."""
from decimal import Decimal as D

from apps.plataforma.services.coordenadas import diagnosticar


def test_dentro_de_colombia_no_es_problema():
    assert diagnosticar(D("6.244203"), D("-75.581211")) is None


def test_vacio_del_todo_no_es_problema():
    assert diagnosticar(None, None) is None


def test_longitud_sin_signo_menos():
    assert diagnosticar(D("9.0911"), D("74.9911")) == ("Longitud sin el signo menos", "9.0911, -74.9911")


def test_invertidas():
    assert diagnosticar(D("-75.5"), D("6.2")) == ("Latitud y longitud invertidas", "6.2, -75.5")


def test_falta_una():
    assert diagnosticar(D("6.2"), None) == ("Falta una de las dos", "")


def test_fuera_de_colombia():
    assert diagnosticar(D("40.4"), D("-3.7"))[0] == "Fuera de Colombia"
