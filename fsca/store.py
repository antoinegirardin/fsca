"""Persistance SQLite : état incrémental + table plate des identifiants pour le rapprochement inventaire."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .keys import identifier_keys, iter_identifiers, reliability, role

SCHEMA = """
CREATE TABLE IF NOT EXISTS publications (
    ref               TEXT PRIMARY KEY,
    status            TEXT,
    publikations_datum TEXT,
    status_datum      TEXT,
    hersteller        TEXT,
    begruendung       TEXT,
    api_json          TEXT NOT NULL,     -- publication brute renvoyée par l'API Swissmedic
    docs_key          TEXT,              -- documents retenus ; change => ré-extraction
    docs_json         TEXT,
    texte             TEXT,              -- texte intégral extrait localement (recherche plein texte)
    regex_json        TEXT,              -- identifiants GS1 détectés par regex
    extraction_json   TEXT,              -- NoticeExtraction produite par Claude
    controles_json    TEXT,
    model             TEXT,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    extracted_at      TEXT,
    error             TEXT,
    updated_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    from_date   TEXT,
    to_date     TEXT,
    n_seen      INTEGER, n_extracted INTEGER, n_skipped INTEGER, n_errors INTEGER,
    dry_run     INTEGER
);
"""

# Tables dérivées de extraction_json : recréées à chaque ouverture, pour suivre
# l'évolution des règles de normalisation (fsca/keys.py) sans migration.
DERIVED_SCHEMA = """
DROP TABLE IF EXISTS cles_recherche;
DROP TABLE IF EXISTS identifiants;
CREATE TABLE identifiants (
    id                INTEGER PRIMARY KEY,
    ref               TEXT NOT NULL,
    dispositif_idx    INTEGER NOT NULL,
    nom_commercial    TEXT,
    type_equipement   TEXT,
    fabricant         TEXT,
    type_id           TEXT NOT NULL,     -- nom_commercial, udi_di_gtin, references_catalogue, lots, plage_serie…
    concerne          TEXT NOT NULL,     -- direct | associé (produit qui définit la configuration à risque)
    role              TEXT NOT NULL,     -- produit | restriction | associé (fsca.keys.role)
    valeur            TEXT NOT NULL,     -- telle qu'écrite dans la notice
    valeur_normalisee TEXT NOT NULL,     -- clé principale (fsca.keys.normalize_identifier)
    fiabilite         TEXT NOT NULL,     -- haute | moyenne | faible | contexte (fsca.keys.reliability)
    plage_debut       TEXT,
    plage_fin         TEXT
);
CREATE INDEX idx_identifiants_ref ON identifiants(ref);
CREATE TABLE cles_recherche (           -- une ligne par variante : jointure exacte avec l'inventaire
    identifiant_id    INTEGER NOT NULL REFERENCES identifiants(id),
    cle               TEXT NOT NULL
);
CREATE INDEX idx_cles ON cles_recherche(cle);
"""


_PLACEHOLDERS = {"N/A", "NA", "-", "--", "NONE", "ALL", "ALLE", "TOUS", "N.A.", "/"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def api_as_extraction(publication: dict) -> dict:
    """Dispositifs saisis dans Swissmedic, mis en forme d'extraction (repli sans extraction Claude)."""
    def split(v: str | None) -> list[str]:
        return [x.strip() for x in re.split(r"[,;\n]", v or "") if x.strip()]

    devices = []
    for dev in publication.get("devices") or []:
        devices.append({
            "nom_commercial": dev.get("handelsname") or "",
            "type_equipement": None,
            "identifiants": {"references_catalogue": split(dev.get("model")), "lots": split(dev.get("lot")),
                             "numeros_serie": split(dev.get("sn")),
                             "versions_logicielles": split(dev.get("swVersion"))},
        })
    return {"fabricant": publication.get("hersteller"), "dispositifs": devices}


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.executescript(DERIVED_SCHEMA)
        for row in self.db.execute("SELECT ref, api_json, extraction_json FROM publications").fetchall():
            extraction = json.loads(row["extraction_json"]) if row["extraction_json"] else None
            self._rebuild_identifiants(row["ref"], json.loads(row["api_json"]), extraction)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # --- état incrémental -------------------------------------------------
    def get(self, ref: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM publications WHERE ref = ?", (ref,)).fetchone()

    def needs_processing(self, ref: str, docs_key: str, want_extraction: bool) -> bool:
        row = self.get(ref)
        if row is None or row["docs_key"] != docs_key or row["error"]:
            return True
        return want_extraction and row["extraction_json"] is None

    def last_successful_to_date(self) -> str | None:
        row = self.db.execute(
            "SELECT to_date FROM runs WHERE finished_at IS NOT NULL AND n_errors = 0 AND dry_run = 0 "
            "AND to_date IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["to_date"] if row else None

    # --- écriture ----------------------------------------------------------
    def save(
        self,
        publication: dict,
        docs_key: str,
        docs: list[dict],
        texte: str | None,
        regex: dict | None,
        extraction: dict | None = None,
        controles: dict | None = None,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        error: str | None = None,
    ) -> None:
        ref = publication["swissmedicRef"]
        previous = self.get(ref)
        extracted_at = now_iso() if extraction is not None else None
        if extraction is None and previous is not None and error is None and previous["docs_key"] == docs_key:
            # dry-run sur une notice déjà extraite : ne pas effacer l'extraction existante
            extraction = json.loads(previous["extraction_json"]) if previous["extraction_json"] else None
            controles = json.loads(previous["controles_json"]) if previous["controles_json"] else controles
            model, input_tokens, output_tokens = previous["model"], previous["input_tokens"], previous["output_tokens"]
            extracted_at = previous["extracted_at"]
        self.db.execute(
            """INSERT INTO publications (ref, status, publikations_datum, status_datum, hersteller, begruendung,
                   api_json, docs_key, docs_json, texte, regex_json, extraction_json, controles_json, model,
                   input_tokens, output_tokens, extracted_at, error, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ref) DO UPDATE SET
                   status=excluded.status, publikations_datum=excluded.publikations_datum,
                   status_datum=excluded.status_datum, hersteller=excluded.hersteller,
                   begruendung=excluded.begruendung, api_json=excluded.api_json, docs_key=excluded.docs_key,
                   docs_json=excluded.docs_json, texte=excluded.texte, regex_json=excluded.regex_json,
                   extraction_json=excluded.extraction_json, controles_json=excluded.controles_json,
                   model=excluded.model, input_tokens=excluded.input_tokens,
                   output_tokens=excluded.output_tokens, extracted_at=excluded.extracted_at,
                   error=excluded.error, updated_at=excluded.updated_at""",
            (
                ref,
                publication.get("status"),
                publication.get("publikationsDatum"),
                publication.get("statusDatum"),
                publication.get("hersteller"),
                publication.get("begruendung") or None,
                json.dumps(publication, ensure_ascii=False),
                docs_key,
                json.dumps(docs, ensure_ascii=False),
                texte,
                json.dumps(regex, ensure_ascii=False) if regex is not None else None,
                json.dumps(extraction, ensure_ascii=False) if extraction is not None else None,
                json.dumps(controles, ensure_ascii=False) if controles is not None else None,
                model,
                input_tokens,
                output_tokens,
                extracted_at,
                error,
                now_iso(),
            ),
        )
        self._rebuild_identifiants(ref, publication, extraction)
        self.db.commit()

    def _rebuild_identifiants(self, ref: str, publication: dict, extraction: dict | None) -> None:
        self.db.execute("DELETE FROM cles_recherche WHERE identifiant_id IN (SELECT id FROM identifiants WHERE ref=?)",
                        (ref,))
        self.db.execute("DELETE FROM identifiants WHERE ref = ?", (ref,))
        source = extraction or api_as_extraction(publication)
        for item in iter_identifiers(source):
            value = (item["valeur"] or "").strip()
            if not value or value.upper() in _PLACEHOLDERS:
                continue
            keys = identifier_keys(item)
            cur = self.db.execute(
                "INSERT INTO identifiants (ref, dispositif_idx, nom_commercial, type_equipement, fabricant, type_id,"
                " concerne, role, valeur, valeur_normalisee, fiabilite, plage_debut, plage_fin)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ref, item["dispositif_idx"], item["nom_commercial"], item["type_equipement"], source.get("fabricant"),
                 item["type_id"], item["concerne"], role(item), value, keys[0] if keys else "", reliability(item["type_id"], value),
                 item.get("plage_debut"),
                 item.get("plage_fin")),
            )
            self.db.executemany("INSERT INTO cles_recherche VALUES (?,?)", [(cur.lastrowid, k) for k in keys])

    # --- runs --------------------------------------------------------------
    def start_run(self, from_date: str | None, to_date: str | None, dry_run: bool) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (started_at, from_date, to_date, dry_run) VALUES (?,?,?,?)",
            (now_iso(), from_date, to_date, int(dry_run)),
        )
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, n_seen: int, n_extracted: int, n_skipped: int, n_errors: int,
                   complete: bool = True) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, n_seen=?, n_extracted=?, n_skipped=?, n_errors=?, "
            "to_date = CASE WHEN ? THEN to_date ELSE NULL END WHERE id=?",
            (now_iso(), n_seen, n_extracted, n_skipped, n_errors, int(complete), run_id),
        )
        self.db.commit()

    def rows_for(self, refs: list[str]) -> list[sqlite3.Row]:
        if not refs:
            return []
        q = f"SELECT * FROM publications WHERE ref IN ({','.join('?' * len(refs))}) ORDER BY status_datum DESC, ref"
        return self.db.execute(q, refs).fetchall()

    def all_rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM publications ORDER BY status_datum DESC, ref").fetchall()
