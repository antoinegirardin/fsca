"""Exports lisibles par Excel et par un LLM local : JSONL, CSV et une fiche Markdown par notice."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from .keys import identifier_keys, iter_identifiers, reliability, role
from .schema import CONSEQUENCES_FROM_GRAVITE

# Identifiants affichés en plus des champs de la fiche (Modèle, Lot, Serial number)
ID_LABELS = {
    "udi_di_gtin": "UDI-DI / GTIN",
    "basic_udi_di": "Basic UDI-DI",
    "versions_logicielles": "Versions logicielles",
    "plages_ou_motifs": "Plages / motifs",
}
NON_RENSEIGNE = "–"


def _unique(values: list[str]) -> list[str]:
    seen, out = set(), []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _plages(ids: dict, kind: str) -> list[str]:
    return [f"{p['debut']} → {p['fin']}" for p in ids.get("plages_numeriques") or [] if p["type"] == kind]


def fiche(ex: dict) -> dict:
    """Champs au format de la fiche de synthèse, une entrée par dispositif.

    Les notices extraites avant l'ajout de type_notice / type_dm / classe_ce ont ces champs à null
    jusqu'à leur ré-extraction ; « Conséquences » est dérivé de la gravité ISO 14971.
    """
    ev = ex["evaluation"]
    devices = []
    for dev in ex["dispositifs"]:
        ids = dev["identifiants"]
        devices.append({
            "nom_commercial": dev["nom_commercial"],
            "type_dm": dev.get("type_dm"),
            "classe_dm_ce": dev.get("classe_ce"),
            "classe_dm_ce_source": dev.get("classe_ce_source"),
            "description": dev["description"],
            "modele": _unique((ids.get("modeles") or []) + (ids.get("references_catalogue") or [])),
            "lot": (ids.get("lots") or []) + _plages(ids, "lot"),
            "serial_number": (ids.get("numeros_serie") or []) + _plages(ids, "serie"),
        })
    return {
        "type_notice": ex.get("type_notice"),
        "consequences": CONSEQUENCES_FROM_GRAVITE.get(ev["gravite_iso14971"]),
        "dispositifs": devices,
    }


def _classe(d: dict) -> str:
    if not d["classe_dm_ce"]:
        return NON_RENSEIGNE
    return d["classe_dm_ce"] + (" (estimée)" if d["classe_dm_ce_source"] == "estimée" else "")


def _liste(values: list[str]) -> str:
    return ", ".join(values) if values else NON_RENSEIGNE


def _record(row: sqlite3.Row) -> dict:
    docs = json.loads(row["docs_json"] or "[]")
    return {
        "swissmedic_ref": row["ref"],
        "statut": row["status"],
        "date_publication": row["publikations_datum"],
        "date_statut": row["status_datum"],
        "motif_mise_a_jour": row["begruendung"],
        "fabricant_swissmedic": row["hersteller"],
        "documents_utilises": docs,
        "fiche": fiche(json.loads(row["extraction_json"])) if row["extraction_json"] else None,
        "swissmedic_api": json.loads(row["api_json"]),
        "extraction": json.loads(row["extraction_json"]) if row["extraction_json"] else None,
        "identifiants_detectes_regex": json.loads(row["regex_json"]) if row["regex_json"] else None,
        "controles": json.loads(row["controles_json"]) if row["controles_json"] else None,
        "modele_llm": row["model"],
        "extrait_le": row["extracted_at"],
        "erreur": row["error"],
    }


def markdown_sheet(rec: dict) -> str:
    ex = rec["extraction"]
    lines = [f"# {rec['swissmedic_ref']} – {ex['titre'] if ex else rec['fabricant_swissmedic']}", ""]
    lines += [
        f"- Statut Swissmedic : {rec['statut']} (publié le {rec['date_publication']}, "
        f"dernière modification {rec['date_statut']})",
    ]
    if rec["motif_mise_a_jour"]:
        lines.append(f"- Motif de mise à jour : {rec['motif_mise_a_jour']}")
    lines.append("- Documents analysés : " + ", ".join(d["title"] for d in rec["documents_utilises"]))
    if not ex:
        lines += ["", "_Pas encore d'extraction structurée._"]
        if rec["erreur"]:
            lines.append(f"Erreur : {rec['erreur']}")
        return "\n".join(lines) + "\n"

    ev = ex["evaluation"]
    fi = rec["fiche"]
    lines += [
        f"- Fabricant : {ex['fabricant']}" + (f" (SRN {ex['srn_fabricant']})" if ex["srn_fabricant"] else ""),
        f"- Référence fabricant : {ex['reference_fabricant'] or '–'} · Date de la notice : {ex['date_notice'] or '–'}",
        "",
        "| Type de notice | Conséquences | Urgence | Classe de rappel (FDA) |",
        "|---|---|---|---|",
        f"| **{fi['type_notice'] or NON_RENSEIGNE}** | **{fi['consequences'] or NON_RENSEIGNE}** "
        f"| {ev['urgence']} | {ev['classe_rappel_estimee']} |",
        "",
        "## Synthèse",
        ex["synthese"],
        "",
        "## Évaluation (estimation automatique)",
        f"- Urgence : **{ev['urgence']}** · Gravité (ISO 14971) : **{ev['gravite_iso14971']}** · "
        f"Probabilité : {ev['probabilite']} · Classe de rappel estimée (FDA) : **{ev['classe_rappel_estimee']}**",
        f"- Arrêt d'utilisation requis : {'oui' if ev['arret_utilisation_requis'] else 'non'} · "
        f"Suivi des patients déjà traités : {'oui' if ev['patients_deja_traites_concernes'] else 'non'}",
        f"- Justification : {ev['justification']}",
        f"- Déclaré par le fabricant : risque {ex.get('risque_declare_fabricant') or '–'} · "
        f"probabilité {ex.get('probabilite_declaree_fabricant') or '–'}",
        "",
        "## Dispositifs concernés",
    ]
    for dev, fd in zip(ex["dispositifs"], fi["dispositifs"]):
        lines += [
            f"### {dev['nom_commercial']}",
            f"- **Type de DM** : {fd['type_dm'] or NON_RENSEIGNE} · **Classe DM CE** : {_classe(fd)}",
            f"- **Description** : {fd['description']}",
            f"- **Modèle** : {_liste(fd['modele'])}",
            f"- **Lot** : {_liste(fd['lot'])}",
            f"- **Serial number** : {_liste(fd['serial_number'])}",
            *([f"- Autres noms : {', '.join(dev['noms_alternatifs'])}"] if dev.get("noms_alternatifs") else []),
            f"- Type d'équipement : {dev['type_equipement']} ({dev['nature']}) · Catégorie EMDN : "
            f"{dev['categorie_emdn']} · Portée : {dev['portee']}",
        ]
        ids = dev["identifiants"]
        for key, label in ID_LABELS.items():
            if ids[key]:
                lines.append(f"- {label} : " + ", ".join(ids[key]))
        if not ids["liste_complete"]:
            lines.append("- ⚠ Liste d'identifiants incomplète (voir la notice ou son annexe)")
        if dev["non_concernes"]:
            lines.append(f"- Non concernés : {dev['non_concernes']}")
        for prod in dev.get("produits_associes") or []:
            refs = f" (réf. {', '.join(prod['references'])})" if prod["references"] else ""
            maker = f", {prod['fabricant']}" if prod["fabricant"] else ""
            lines.append(f"- Produit associé : {prod['nom']}{maker}{refs} — {prod['role']}")
        lines.append("")
    lines += ["## Problème", ex["probleme"], ""]
    if ex["cause"]:
        lines += ["## Cause", ex["cause"], ""]
    lines += ["## Risque", ex["risque"], ""]
    if ex["incidents_signales"]:
        lines += [f"Incidents signalés : {ex['incidents_signales']}", ""]
    lines += ["## Actions demandées aux utilisateurs"]
    for a in ex["actions_utilisateur"]:
        lines.append(f"- [{a['type']}] {a['detail']}" + (f" — délai : {a['delai']}" if a["delai"] else ""))
    if ex["actions_fabricant"]:
        lines += ["", "## Actions du fabricant"] + [f"- {a}" for a in ex["actions_fabricant"]]
    lines += [
        "",
        f"Types d'action : {', '.join(ex['types_action_fsca'])}",
        f"Destinataires : {', '.join(ex['destinataires'])}",
        f"Accusé de réception requis : {'oui' if ex['accuse_reception_requis'] else 'non'}"
        + (f" ({ex['delai_accuse_reception']})" if ex["delai_accuse_reception"] else ""),
    ]
    if ex["contact"]:
        lines.append(f"Contact : {ex['contact']}")
    ctrl = rec["controles"] or {}
    if ctrl.get("gtin_absents_extraction"):
        lines.append("⚠ GTIN détectés dans le texte mais absents de l'extraction : "
                     + ", ".join(ctrl["gtin_absents_extraction"]))
    if ex["remarques_extraction"]:
        lines += ["", f"_Remarques d'extraction : {ex['remarques_extraction']}_"]
    return "\n".join(lines) + "\n"


# CSV de filtrage : une ligne par identifiant capable de désigner un produit dans un inventaire
# (rôle « produit » ou « associé »). Les restrictions (lots, séries, versions, plages) ne désignent
# rien seules : elles sont recopiées en colonnes sur chaque ligne du dispositif, pour confirmer
# une correspondance. Ordre des colonnes = hiérarchie notice → dispositif → identifiant.
CSV_FIELDS = [
    "swissmedic_ref", "dispositif_id", "nom_commercial", "role", "type_id", "valeur", "fiabilite",
    "cles_recherche",
    "restriction_lots", "restriction_numeros_serie", "restriction_versions", "restriction_plages",
    "restriction_autres", "liste_complete", "non_concernes", "produit_associe_role",
    "type_notice", "consequences", "type_dm", "classe_dm_ce",
    "fabricant", "type_equipement", "nature", "categorie_emdn", "portee",
    "statut", "date_publication", "date_statut", "titre",
    "urgence", "gravite_iso14971", "probabilite", "classe_rappel_estimee", "arret_utilisation_requis",
    "patients_deja_traites_concernes", "risque_declare_fabricant", "actions_utilisateur",
    "accuse_reception_requis", "synthese", "documents", "erreur",
]


def _actions(ex: dict) -> str:
    return " ; ".join(a["type"] + (f" ({a['delai']})" if a["delai"] else "") for a in ex["actions_utilisateur"])


def csv_rows(rec: dict):
    ref = rec["swissmedic_ref"]
    notice = {
        "swissmedic_ref": ref, "statut": rec["statut"],
        "date_publication": rec["date_publication"], "date_statut": rec["date_statut"],
        "documents": " | ".join(d["url"] for d in rec["documents_utilises"]), "erreur": rec["erreur"],
        "fabricant": rec["fabricant_swissmedic"],
    }
    ex = rec["extraction"]
    if not ex:
        yield notice  # notice sans extraction (erreur) : une ligne pour qu'elle reste visible
        return
    ev = ex["evaluation"]
    fi = rec["fiche"]
    notice.update({
        "type_notice": fi["type_notice"], "consequences": fi["consequences"],
        "fabricant": ex["fabricant"], "titre": ex["titre"], "synthese": ex["synthese"],
        "urgence": ev["urgence"], "gravite_iso14971": ev["gravite_iso14971"], "probabilite": ev["probabilite"],
        "classe_rappel_estimee": ev["classe_rappel_estimee"],
        "arret_utilisation_requis": ev["arret_utilisation_requis"],
        "patients_deja_traites_concernes": ev["patients_deja_traites_concernes"],
        "risque_declare_fabricant": ex.get("risque_declare_fabricant"),
        "actions_utilisateur": _actions(ex), "accuse_reception_requis": ex["accuse_reception_requis"],
    })

    devices = {}
    for i, dev in enumerate(ex["dispositifs"]):
        ids = dev["identifiants"]
        devices[i] = {
            "dispositif_id": f"{ref}-{i + 1}", "nom_commercial": dev["nom_commercial"],
            "type_dm": fi["dispositifs"][i]["type_dm"], "classe_dm_ce": _classe(fi["dispositifs"][i]),
            "type_equipement": dev.get("type_equipement"),
            **{k: dev.get(k) for k in ("nature", "categorie_emdn", "portee", "non_concernes")},
            "restriction_lots": " | ".join(ids.get("lots") or []),
            "restriction_numeros_serie": " | ".join(ids.get("numeros_serie") or []),
            "restriction_versions": " | ".join(ids.get("versions_logicielles") or []),
            "restriction_plages": " | ".join(
                f"{'lot' if p['type'] == 'lot' else 'série'} {p['debut']} → {p['fin']}"
                for p in ids.get("plages_numeriques") or []),
            "restriction_autres": " | ".join(ids.get("plages_ou_motifs") or []),
            "liste_complete": ids["liste_complete"],
        }

    associated = {}  # produits associés dédoublonnés au niveau de la notice
    for item in iter_identifiers(ex):
        r = role(item)
        if r == "restriction":
            continue
        keys = identifier_keys(item)
        row = {**notice, **devices[item["dispositif_idx"]], "role": r, "type_id": item["type_id"],
               "valeur": item["valeur"], "fiabilite": reliability(item["type_id"], item["valeur"]),
               "cles_recherche": " | ".join(keys), "produit_associe_role": item.get("produit_associe_role")}
        if r == "associé":
            # Les restrictions (lots, versions…) portent sur le dispositif concerné, pas sur le produit tiers.
            row.update({k: "" for k in ("restriction_lots", "restriction_numeros_serie", "restriction_versions",
                                        "restriction_plages", "restriction_autres", "liste_complete",
                                        "non_concernes", "portee", "nature", "categorie_emdn", "type_equipement",
                                        "type_dm", "classe_dm_ce")})
            sig = (item["type_id"], keys[0] if keys else item["valeur"])
            if sig in associated:
                prev = associated[sig]
                prev["dispositif_id"] += f" | {row['dispositif_id']}"
                prev["nom_commercial"] += f" | {row['nom_commercial']}"
                continue
            associated[sig] = row
            continue
        yield row
    yield from associated.values()


def export(rows: list[sqlite3.Row], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "fiches").mkdir(exist_ok=True)
    records = [_record(r) for r in rows]
    paths = {"jsonl": out_dir / "notices.jsonl", "csv": out_dir / "identifiants.csv", "fiches": out_dir / "fiches"}

    with paths["jsonl"].open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # utf-8-sig + ';' : ouverture directe et correcte dans Excel (paramètres régionaux CH/FR)
    with paths["csv"].open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for rec in records:
            (paths["fiches"] / f"{rec['swissmedic_ref']}.md").write_text(markdown_sheet(rec), encoding="utf-8")
            w.writerows(csv_rows(rec))
    return paths
