"""Tests de la lógica pura del pipeline TSF (sin red ni DB).

La lógica de enriquecimiento (Sun Factory, generación, estimaciones) vive en
`app/services/tsf_sync.py`; el router `proximos_energizar` solo lee/escribe en la
BD. Por eso estos tests apuntan al servicio.
"""
from datetime import date

import pytest

from app.services import tsf_sync as pe


# ── _parse_iso_date ─────────────────────────────────────────────────────────────

def test_parse_iso_date_with_z():
    assert pe._parse_iso_date("2026-05-25T17:00:00Z") == date(2026, 5, 25)


def test_parse_iso_date_date_only():
    assert pe._parse_iso_date("2026-01-14") == date(2026, 1, 14)


@pytest.mark.parametrize("bad", [None, "", "no-es-fecha", "2026-13-99"])
def test_parse_iso_date_invalid_returns_none(bad):
    assert pe._parse_iso_date(bad) is None


# ── _ENERG_MILESTONE_RE ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "Hito 5 - RETIE y legalización",
    "HITO 4. RETIE Y LEGALIZACIÓN",
    "Energización del proyecto",
    "Puesta en marcha",
])
def test_energ_regex_matches(name):
    assert pe._ENERG_MILESTONE_RE.search(name)


@pytest.mark.parametrize("name", [
    "Hito 1. Ingeniería de detalle",
    "Equipos principales en puerto",
    "Beneficios tributarios",
])
def test_energ_regex_no_match(name):
    assert not pe._ENERG_MILESTONE_RE.search(name)


# ── _pick_energization_milestone (núcleo) ───────────────────────────────────────

def _ms(name, planned, dt, pct=None):
    return {"name": name, "planned_date": planned, "date": dt,
            "progress": {"calculated_percentage": pct} if pct is not None else {}}


def test_pick_prefers_retie_by_name_even_if_not_last():
    milestones = [
        _ms("Hito 5 - RETIE y legalización", "2025-12-30T17:00:00Z", "2026-02-16T17:00:00Z", 37.04),
        _ms("Hito 6 - Cierre administrativo", "2026-03-30T17:00:00Z", "2026-04-01T17:00:00Z", 0.0),
    ]
    got = pe._pick_energization_milestone(milestones)
    assert got["energization_date"] == date(2026, 2, 16)
    assert got["avance_pct"] == 37.04
    assert "RETIE" in got["milestone"]


def test_pick_falls_back_to_final_milestone_when_no_name_match():
    milestones = [
        _ms("Hito 1. Ingeniería", "2025-08-05T17:00:00Z", "2025-09-01T17:00:00Z", 100),
        _ms("Hito 3. Equipos", "2025-09-26T17:00:00Z", "2025-12-10T17:00:00Z", 50),
    ]
    got = pe._pick_energization_milestone(milestones)
    # el de mayor planned_date
    assert got["energization_date"] == date(2025, 12, 10)


def test_pick_uses_planned_date_when_no_projected_date():
    milestones = [_ms("RETIE", "2026-07-01T17:00:00Z", None, 10)]
    got = pe._pick_energization_milestone(milestones)
    assert got["energization_date"] == date(2026, 7, 1)


def test_pick_avance_falls_back_to_activity_percentage():
    m = {"name": "RETIE", "planned_date": "2026-07-01T00:00:00Z", "date": None,
         "progress": {"activity_percentage": 22.5}}
    assert pe._pick_energization_milestone([m])["avance_pct"] == 22.5


def test_pick_none_when_empty():
    assert pe._pick_energization_milestone([]) is None


def test_pick_none_when_no_dated_milestones():
    assert pe._pick_energization_milestone([{"name": "RETIE", "progress": {}}]) is None


def test_pick_real_project_103_shape():
    """Réplica de los 7 hitos reales del proyecto 103 (COLCEST55P2)."""
    milestones = [
        _ms("Hito 1. Ingeniería de detalle", "2025-08-05T17:00:00Z", "2025-11-11T17:00:00Z", 100),
        _ms("Hito 2. Equipos en puerto", "2025-08-29T17:00:00Z", "2025-08-29T17:00:00Z", 100),
        _ms("Hito 3. Equipos en el proyecto", "2025-09-26T17:00:00Z", "2025-12-10T17:00:00Z", 80),
        _ms("Hito 4 - Instalación del proyecto", "2025-11-10T17:00:00Z", "2026-05-27T17:00:00Z", 40),
        _ms("Hito 5 - RETIE y legalización", "2025-12-30T17:00:00Z", "2026-02-16T17:00:00Z", 37.04),
    ]
    got = pe._pick_energization_milestone(milestones)
    assert got["milestone"] == "Hito 5 - RETIE y legalización"
    assert got["energization_date"] == date(2026, 2, 16)
    assert got["avance_pct"] == 37.04


# ── _derive_commercial_name ─────────────────────────────────────────────────────

@pytest.mark.parametrize("code,expected", [
    ("COLSUCT3P1_MORROA_SUR", "Morroa Sur"),      # prefijo de código con dígitos → se descarta
    ("COLBOLT2P3_LA_UNION", "La Union"),
    ("COLSUCT3P1_MORROA_SUR_2", "Morroa Sur 2"),  # el sitio puede tener sufijo numérico
    ("MORROSQUILLO_2", "Morrosquillo 2"),         # 1er token sin pinta de código → se conserva
    ("SINGLEWORD", "Singleword"),                  # sin separador
])
def test_derive_commercial_name(code, expected):
    assert pe._derive_commercial_name(code) == expected


@pytest.mark.parametrize("bad", [None, ""])
def test_derive_commercial_name_empty(bad):
    assert pe._derive_commercial_name(bad) == ""


# ── _sunfactory_token: reúso de credenciales Solenium ───────────────────────────

class _FakeResp:
    def raise_for_status(self): pass
    def json(self): return {"access": "tok-123"}


class _FakeClient:
    last_json = None
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, url, json=None):
        _FakeClient.last_json = json
        return _FakeResp()


def _set(monkeypatch, **vals):
    for k, v in vals.items():
        monkeypatch.setattr(pe.settings, k, v, raising=False)


def test_sunfactory_token_falls_back_to_solenium_creds(monkeypatch):
    # Sin credenciales SUNFACTORY_* dedicadas → debe reusar SOLENIUM_USER/PASS.
    _set(monkeypatch, SUNFACTORY_USERNAME="", SUNFACTORY_PASSWORD="",
         SUNFACTORY_AUTH_URL="https://auth.sole.tech/api/token/",
         SOLENIUM_USER="sol-user", SOLENIUM_PASS="sol-pass")
    monkeypatch.setattr(pe.httpx, "Client", _FakeClient)
    assert pe._sunfactory_token() == "tok-123"
    assert _FakeClient.last_json == {"username": "sol-user", "password": "sol-pass"}


def test_sunfactory_token_prefers_dedicated_creds(monkeypatch):
    # Si SUNFACTORY_* están seteadas, ganan sobre las de Solenium.
    _set(monkeypatch, SUNFACTORY_USERNAME="sf-user", SUNFACTORY_PASSWORD="sf-pass",
         SUNFACTORY_AUTH_URL="https://auth.sole.tech/api/token/",
         SOLENIUM_USER="sol-user", SOLENIUM_PASS="sol-pass")
    monkeypatch.setattr(pe.httpx, "Client", _FakeClient)
    assert pe._sunfactory_token() == "tok-123"
    assert _FakeClient.last_json["username"] == "sf-user"


def test_sunfactory_token_none_without_any_creds(monkeypatch):
    _set(monkeypatch, SUNFACTORY_USERNAME="", SUNFACTORY_PASSWORD="",
         SOLENIUM_USER="", SOLENIUM_PASS="")
    assert pe._sunfactory_token() is None


# ── Mapeo fase ↔ etiqueta ───────────────────────────────────────────────────────

def test_status_to_fase_roundtrip():
    for label, slug in pe._STATUS_TO_FASE.items():
        assert pe._FASE_TO_LABEL[slug] == label


def test_fase_slugs_known():
    assert set(pe._STATUS_TO_FASE.values()) == {
        "en_construccion", "pruebas", "proximo_energizar", "energizado",
    }


# ── sync_tsf_projects (upsert) — DB falsa, sin red ──────────────────────────────

class _FakeResult:
    def __init__(self, row):
        self._row = row
    def first(self):
        return self._row
    def fetchall(self):
        # Usado por _buscar_candidato_similar (busqueda de posibles vinculos).
        return [] if self._row is None else [self._row]
    def all(self):
        # Usado por sync_tsf_projects (deteccion de ambiguedad, sin LIMIT 1).
        return [] if self._row is None else [self._row]


class _ExistingRow:
    def __init__(self, id, estado="en_desarrollo"):
        self.id = id
        self.estado = estado


class _FakeDB:
    """Captura las sentencias ejecutadas y simula el SELECT de existencia."""
    def __init__(self, existing=None):
        self.existing = existing  # _ExistingRow o None
        self.statements = []      # [(sql, params)]

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.statements.append((sql, params or {}))
        if sql.strip().upper().startswith("SELECT"):
            return _FakeResult(self.existing)
        return _FakeResult(None)

    def commit(self): pass
    def rollback(self): pass


def _one_project():
    return [{
        "origina_code": "COLSUCT3P1_MORROA_SUR",
        "base_name": "COLSUCT3P1_MORROA_SUR",
        "tsf_code": "COLSUCT3P1",
        "commercial_name": "Morroa Sur",
        "status": "Próximo a energizar",
        "stage": "deploy",
        "energization_date": date(2026, 9, 1),
        "energization_source": "sunfactory",
        "avance_pct": 88.5,
        "monthly_mwh": 127.71,
        "installed_power_kwp": 990,
        "already_generating": False,
        "municipio": "Morroa", "departamento": "Sucre",
        "latitud": None, "longitud": None,
    }]


def _patch_fetch(monkeypatch, projects):
    # sync_tsf_projects ahora lee de Sun Factory (fuente principal).
    monkeypatch.setattr(pe, "fetch_sunfactory_projects", lambda **k: (projects, []))


def test_sync_no_crea_cuando_no_existe_ya_no_inserta(monkeypatch):
    # sync_tsf_projects ya NO crea proyectos -- eso quedo en /proyectos/pendientes
    # (confirmacion humana). Sin match ni candidato parecido, solo se cuenta.
    _patch_fetch(monkeypatch, _one_project())
    db = _FakeDB(existing=None)
    stats = pe.sync_tsf_projects(db)
    assert stats["creados"] == 0 and stats["actualizados"] == 0
    assert stats["sin_match"] == 1
    inserts = [s for s, _ in db.statements if "INSERT INTO proyectos" in s]
    assert not inserts


# ── _tsf_code_from_base_name ────────────────────────────────────────────────────

@pytest.mark.parametrize("base_name,expected", [
    ("COLSUCT3P1_MORROA_SUR", "COLSUCT3P1"),
    ("COLCEST55P2_VALLEDUPAR_NORTE", "COLCEST55P2"),
    ("COLSUCT49P1_SAN-LUIS-DE-SINCE_OCCIDENTE", "COLSUCT49P1"),
    ("SMGS_0006_FEN5_Barrancabermeja", None),  # no es código CREG
    (None, None),
    ("", None),
])
def test_tsf_code_from_base_name(base_name, expected):
    assert pe._tsf_code_from_base_name(base_name) == expected


def test_sync_update_links_codigo_tsf_and_origina_code():
    # Proyecto ya existente (registrado a mano con codigo_tsf) → el sync lo
    # ACTUALIZA (no duplica) y enlaza origina_code/codigo_tsf vía COALESCE.
    import types
    projects = _one_project()
    db = _FakeDB(existing=_ExistingRow(id=7))
    # parchear el fetch directamente (sin monkeypatch fixture)
    orig = pe.fetch_sunfactory_projects
    pe.fetch_sunfactory_projects = lambda **k: (projects, [])
    try:
        stats = pe.sync_tsf_projects(db)
    finally:
        pe.fetch_sunfactory_projects = orig
    assert stats["actualizados"] == 1 and stats["creados"] == 0
    upd_sql = next(s for s, _ in db.statements if s.strip().upper().startswith("UPDATE"))
    assert "origina_code = COALESCE(origina_code, :code)" in upd_sql
    assert "codigo_tsf = COALESCE(codigo_tsf, :tsf)" in upd_sql


def test_sync_no_crea_aunque_traiga_sunfactory_project_id(monkeypatch):
    # Igual que arriba: sin match existente, ya no crea -- solo se cuenta en sin_match.
    proj = _one_project()
    proj[0]["solenium_id"] = 106
    _patch_fetch(monkeypatch, proj)
    db = _FakeDB(existing=None)
    stats = pe.sync_tsf_projects(db)
    assert stats["creados"] == 0
    assert stats["sin_match"] == 1
    inserts = [s for s, _ in db.statements if "INSERT INTO proyectos" in s]
    assert not inserts


def test_sync_no_regresa_fase_si_ya_esta_en_operacion(monkeypatch):
    # Bug real: un proyecto que Proyectos pendientes ya marcó como
    # estado='en_operacion' (evidencia real de Quoia/Solenium) no debe volver
    # a "proximo_energizar"/"en_construccion" solo porque Sun Factory todavía
    # no actualizó su propio tracker de obra -- el estado real manda.
    _patch_fetch(monkeypatch, _one_project())  # status "Próximo a energizar" -> fase != energizado
    db = _FakeDB(existing=_ExistingRow(id=7, estado="en_operacion"))
    stats = pe.sync_tsf_projects(db)
    assert stats["actualizados"] == 1
    upd_sql, upd_params = next((s, p) for s, p in db.statements if s.strip().upper().startswith("UPDATE"))
    assert "fase_construccion = :fase" not in upd_sql
    assert "avance_obra_pct = COALESCE" in upd_sql  # el resto de los campos si se sincroniza


def test_sync_si_actualiza_fase_cuando_no_esta_en_operacion(monkeypatch):
    _patch_fetch(monkeypatch, _one_project())
    db = _FakeDB(existing=_ExistingRow(id=7, estado="en_desarrollo"))
    pe.sync_tsf_projects(db)
    upd_sql, upd_params = next((s, p) for s, p in db.statements if s.strip().upper().startswith("UPDATE"))
    assert "fase_construccion = :fase" in upd_sql
    assert upd_params["fase"] == "proximo_energizar"


def test_sync_si_actualiza_fase_a_energizado_aunque_ya_este_en_operacion(monkeypatch):
    # Sin conflicto real: si Sun Factory YA dice "Energizado", se aplica igual.
    proj = _one_project()
    proj[0]["status"] = "Energizado"
    _patch_fetch(monkeypatch, proj)
    db = _FakeDB(existing=_ExistingRow(id=7, estado="en_operacion"))
    pe.sync_tsf_projects(db)
    upd_sql, upd_params = next((s, p) for s, p in db.statements if s.strip().upper().startswith("UPDATE"))
    assert "fase_construccion = :fase" in upd_sql
    assert upd_params["fase"] == "energizado"


def test_sync_update_backfills_sunfactory_project_id():
    proj = _one_project()
    proj[0]["solenium_id"] = 106
    db = _FakeDB(existing=_ExistingRow(id=7))
    orig = pe.fetch_sunfactory_projects
    pe.fetch_sunfactory_projects = lambda **k: (proj, [])
    try:
        stats = pe.sync_tsf_projects(db)
    finally:
        pe.fetch_sunfactory_projects = orig
    assert stats["actualizados"] == 1
    upd_sql, upd_params = next((s, p) for s, p in db.statements if s.strip().upper().startswith("UPDATE"))
    assert "sunfactory_project_id = COALESCE(sunfactory_project_id" in upd_sql
    assert upd_params["sol_id"] == 106


class _FakeDBMatchByIdOnly:
    """Simula que el codigo/base_name YA CAMBIO en Sun Factory (ya no matchea por
    texto), pero el sunfactory_project_id sigue siendo el mismo. Reproduce el bug
    de Monterrubio (id 210 vs 252, jul-2026) y confirma que el fix (match por id
    estable) evita crear un duplicado."""
    def __init__(self, existing_id, existing_sol_id):
        self.existing_id = existing_id
        self.existing_sol_id = existing_sol_id
        self.statements = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        self.statements.append((sql, params))
        if sql.strip().upper().startswith("SELECT"):
            if params.get("sol_id") == self.existing_sol_id:
                return _FakeResult(_ExistingRow(id=self.existing_id))
            return _FakeResult(None)
        return _FakeResult(None)

    def commit(self): pass
    def rollback(self): pass


def test_sync_no_duplica_cuando_sun_factory_cambia_el_codigo(monkeypatch):
    proj = _one_project()
    proj[0]["origina_code"] = "SF-106"   # codigo NUEVO, distinto del que ya estaba guardado
    proj[0]["base_name"] = None          # Sun Factory ya no manda base_name para este proyecto
    proj[0]["tsf_code"] = None
    proj[0]["solenium_id"] = 106
    _patch_fetch(monkeypatch, proj)

    db = _FakeDBMatchByIdOnly(existing_id=210, existing_sol_id=106)
    stats = pe.sync_tsf_projects(db)

    assert stats["creados"] == 0, "no debio crear un duplicado: el sunfactory_project_id ya existia"
    assert stats["actualizados"] == 1


def test_sync_updates_date(monkeypatch):
    _patch_fetch(monkeypatch, _one_project())
    db = _FakeDB(existing=_ExistingRow(id=42))
    stats = pe.sync_tsf_projects(db)
    assert stats["actualizados"] == 1 and stats["creados"] == 0
    upd_sql, upd_params = next((s, p) for s, p in db.statements if s.strip().upper().startswith("UPDATE"))
    assert "fecha_estimada_energizacion" in upd_sql  # sí actualiza la fecha
    assert upd_params["energ"] == date(2026, 9, 1)


# ── Sugerencia de vínculo cuando no hay match exacto (fix 2) ────────────────────

class _FakeDBConCandidato:
    """Simula: ningun match exacto por id/codigo (proyecto nunca sincronizado),
    pero SI hay un proyecto ya existente con nombre parecido -- típicamente creado
    a mano, sin ningún código de Sun Factory nunca registrado."""
    def __init__(self, candidato_row=None):
        self.candidato_row = candidato_row
        self.statements = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        self.statements.append((sql, params))
        if "SELECT id, estado FROM proyectos" in sql:
            return _FakeResult(None)  # ningun match exacto
        if "nombre_comercial, municipio" in sql:
            return _FakeResult(self.candidato_row)
        return _FakeResult(None)

    def commit(self): pass
    def rollback(self): pass


def test_sync_sugiere_vinculo_en_vez_de_duplicar(monkeypatch):
    import types
    proj = _one_project()
    proj[0]["commercial_name"] = "Morroa Sur"
    proj[0]["municipio"] = "Morroa"
    proj[0]["solenium_id"] = 999
    _patch_fetch(monkeypatch, proj)

    candidato = types.SimpleNamespace(id=55, nombre_comercial="Minigranja Morroa Sur (manual)",
                                       municipio="Morroa", sunfactory_project_id=None)
    db = _FakeDBConCandidato(candidato_row=candidato)
    stats = pe.sync_tsf_projects(db)

    assert stats["creados"] == 0, "no debio crear un proyecto nuevo habiendo un candidato parecido"
    assert stats["actualizados"] == 0
    assert len(stats["sugerencias_vinculo"]) == 1
    sug = stats["sugerencias_vinculo"][0]
    assert sug["candidato_id"] == 55
    assert sug["sunfactory_project_id"] == 999
    assert sug["candidato_sunfactory_id_previo"] is None
    inserts = [s for s, _ in db.statements if "INSERT INTO proyectos" in s]
    assert not inserts


def test_sync_sugiere_vinculo_aunque_el_candidato_ya_tenga_otro_id(monkeypatch):
    # Caso real "Monterrubio": Sun Factory reporta el mismo proyecto bajo dos ids
    # propios (106 y 111). El candidato ya quedo vinculado al 111 en una corrida
    # anterior -- antes esto lo excluia de la busqueda y el sync volvia a crear
    # un duplicado en silencio. Ahora debe seguir sugiriendolo (no duplicar).
    import types
    proj = _one_project()
    proj[0]["commercial_name"] = "Minigranja - Monterrubio"
    proj[0]["municipio"] = "La Paz"
    proj[0]["solenium_id"] = 106
    _patch_fetch(monkeypatch, proj)

    candidato = types.SimpleNamespace(id=210, nombre_comercial="Minigranja 0029 - Monterrubio",
                                       municipio="La Paz", sunfactory_project_id=111)
    db = _FakeDBConCandidato(candidato_row=candidato)
    stats = pe.sync_tsf_projects(db)

    assert stats["creados"] == 0, "no debio duplicar: el candidato ya vinculado a otro id sigue siendo sugerido"
    assert len(stats["sugerencias_vinculo"]) == 1
    sug = stats["sugerencias_vinculo"][0]
    assert sug["candidato_id"] == 210
    assert sug["candidato_sunfactory_id_previo"] == 111


def test_sync_no_crea_cuando_no_hay_candidato_parecido(monkeypatch):
    # Sin match exacto y sin candidato de nombre parecido -- ya no crea, solo cuenta.
    proj = _one_project()
    proj[0]["solenium_id"] = 999
    _patch_fetch(monkeypatch, proj)

    db = _FakeDBConCandidato(candidato_row=None)
    stats = pe.sync_tsf_projects(db)

    assert stats["creados"] == 0
    assert stats["sin_match"] == 1
    assert stats["sugerencias_vinculo"] == []


def test_buscar_candidato_similar_ignora_municipio_mal_cargado(monkeypatch):
    # Bug real: "El Paso Norte" (id=92) tenia guardado el DEPARTAMENTO ("Cesar")
    # en vez del municipio real ("El Paso"). Exigir que coincidiera descartaba el
    # match de nombre correcto y terminaba creando un duplicado en silencio.
    # El municipio ya NO se usa como filtro, solo el nombre.
    import types

    class _DB:
        def execute(self, stmt, params=None):
            rows = [types.SimpleNamespace(id=92, nombre_comercial="MGS 0032 - EL Paso Norte", municipio="Cesar")]
            return types.SimpleNamespace(fetchall=lambda: rows)

    resultado = pe._buscar_candidato_similar(_DB(), "Minigranja 0032 - El Paso Norte", "El Paso")
    assert resultado is not None and resultado.id == 92


def test_buscar_candidato_similar_encuentra_por_substring():
    import types

    class _DB:
        def execute(self, stmt, params=None):
            rows = [types.SimpleNamespace(id=7, nombre_comercial="Minigranja - Monterrubio", municipio="La Paz")]
            return types.SimpleNamespace(fetchall=lambda: rows)

    resultado = pe._buscar_candidato_similar(_DB(), "Minigranja 0029 - Monterrubio", "La Paz")
    assert resultado is not None and resultado.id == 7
