"""Clés de recherche des identifiants, pour le rapprochement avec des inventaires hétérogènes.

Un même identifiant peut être noté de plusieurs façons selon l'inventaire :
GTIN sur 13 ou 14 chiffres, référence avec ou sans préfixe « REF » / « Art. »,
avec ou sans zéros de tête, version « v3.1.0 » ou « 3.1 », nom avec ou sans ™, etc.

`search_keys(type_id, valeur)` renvoie toutes les formes à tester, la forme
principale (`normalize_identifier`) en premier. Le côté inventaire doit appliquer
les MÊMES fonctions à ses propres valeurs, puis comparer les clés à l'égalité.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterator

from .pdftext import gtin_is_valid, normalize_gtin

# Types d'identifiants, dans l'ordre d'export
ID_TYPES = (
    "udi_di_gtin",
    "basic_udi_di",
    "references_catalogue",
    "modeles",
    "versions_logicielles",
    "lots",
    "numeros_serie",
)
NAME_TYPES = ("nom_commercial", "nom_alternatif", "modeles", "produit_associe_nom")
# Identifiants qui ne désignent pas un produit à eux seuls : ils restreignent un dispositif
# déjà trouvé (« YY3462 » concerné seulement pour le lot 4541819).
RESTRICTION_TYPES = ("lots", "numeros_serie", "versions_logicielles", "plage_lot", "plage_serie")


def role(item: dict) -> str:
    """produit : une correspondance suffit à désigner le produit ; restriction : condition en plus ;
    associé : produit tiers de la configuration à risque."""
    if item["concerne"] == "associé":
        return "associé"
    return "restriction" if item["type_id"] in RESTRICTION_TYPES else "produit"

# Préfixes retirés seulement s'ils sont suivis d'un séparateur (« REF 123 », « Lot: 42 »)
_PREFIXES = {
    "references_catalogue": r"REF|ART(?:ICLE)?\.?(?:\s*N[O°R]\.?)?|CAT(?:ALOG(?:UE)?)?\.?(?:\s*N[O°R]\.?)?"
    r"|P/?N|PART\s*(?:NO|NUMBER)|ITEM(?:\s*NO)?|CODE|N°|NO\.?|NR\.?|BEST\.?-?NR\.?",
    "produit_associe_reference": r"REF|ART(?:ICLE)?\.?(?:\s*N[O°R]\.?)?|CAT\.?|P/?N|N°|NO\.?|NR\.?",
    "lots": r"LOT|BATCH|CH\.?-?B\.?|CHARGE|N°\s*DE\s*LOT|LOT\s*N[O°]\.?",
    "numeros_serie": r"S/?N|SERIAL(?:\s*(?:NO|NUMBER))?|N°\s*DE\s*S[EÉ]RIE|SERIENNUMMER|SER\.?-?NR\.?",
    "versions_logicielles": r"VERSION|VERS\.?|VER\.?|SW|FW|SOFTWARE|FIRMWARE|LOGICIEL",
    "modeles": r"MOD[EÈ]LE|MODEL|TYPE",
}
_PREFIX_RE = {k: re.compile(rf"^(?:{v})\s*[:.#]?\s+|^(?:{v})\s*[:#]\s*", re.I) for k, v in _PREFIXES.items()}


def fold(value: str) -> str:
    """Majuscules, sans accents ni symboles de marque."""
    v = unicodedata.normalize("NFKD", re.sub(r"[™®©]", "", value))  # avant NFKD, qui changerait ™ en « TM »
    v = "".join(c for c in v if not unicodedata.combining(c))
    return v.upper().strip()


def compact(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", fold(value))


def words(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", fold(value)).strip()


def strip_prefix(type_id: str, value: str) -> str:
    rx = _PREFIX_RE.get(type_id)
    return rx.sub("", value.strip(), count=1) if rx else value.strip()


def _version_core(value: str) -> str:
    v = re.sub(r"\s", "", fold(strip_prefix("versions_logicielles", value)))
    return re.sub(r"^V(?=\d)", "", v)  # « V3.1 » -> « 3.1 »


def normalize_identifier(type_id: str, value: str) -> str:
    """Clé principale (une seule par identifiant)."""
    v = value.strip()
    if type_id == "udi_di_gtin":
        digits = re.sub(r"\D", "", re.sub(r"^\(01\)", "", v))
        if gtin_is_valid(digits):
            return normalize_gtin(digits)
    if type_id == "versions_logicielles":  # les points sont signifiants : 1.1.0 ≠ 11.0
        return _version_core(v)
    if type_id in NAME_TYPES:
        return words(v)
    return compact(strip_prefix(type_id, v))


def search_keys(type_id: str, value: str) -> list[str]:
    """Toutes les formes à tester dans un inventaire, la clé principale en premier."""
    v = value.strip()
    keys = [normalize_identifier(type_id, v)]
    if type_id == "udi_di_gtin":
        g = keys[0]
        if g.isdigit() and len(g) == 14:
            # Les zéros de tête ne changent pas la clé de contrôle : GTIN-13 (EAN) et GTIN-12 (UPC)
            if g.startswith("0"):
                keys.append(g[1:])
            if g.startswith("00"):
                keys.append(g[2:])
            keys.append(f"01{g}")  # forme concaténée d'un code GS1 sans parenthèses
    elif type_id == "versions_logicielles":
        core = keys[0]
        keys.append("V" + core)
        short = core
        while re.fullmatch(r"\d+\.\d+(\.0)+", short):  # « 3.1.0 » ~ « 3.1 », sans descendre sous X.Y
            short = short[:-2]
        keys.append(short)
        if re.fullmatch(r"\d+\.\d+", core):
            keys.append(core + ".0")  # « 3.1 » ~ « 3.1.0 »
    elif type_id in NAME_TYPES:
        keys.append(compact(v))
    else:
        raw, stripped = compact(v), compact(strip_prefix(type_id, v))
        keys += [raw, stripped]
        if re.fullmatch(r"0+[1-9A-Z][0-9A-Z]*", stripped):
            keys.append(stripped.lstrip("0"))  # « 000123 » ~ « 123 »
        if re.fullmatch(r"[A-Z0-9]+[-./ ][A-Z0-9-./ ]+", fold(strip_prefix(type_id, v))):
            keys.append(fold(strip_prefix(type_id, v)))  # forme lisible avec séparateurs d'origine
    seen, out = set(), []
    for k in keys:
        if k and len(k) >= 2 and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def iter_identifiers(extraction: dict) -> Iterator[dict]:
    """Aplatit une extraction (forme stockée) : une entrée par identifiant à rechercher.

    `concerne` vaut "direct" pour le dispositif visé par la notice, "associé" pour un
    produit tiers qui définit la configuration à risque sans être lui-même défectueux.
    Les doublons (même dispositif, même type, même clé principale) sont écartés.
    """
    seen = set()
    for item in _iter_raw(extraction):
        keys = identifier_keys(item)
        sig = (item["dispositif_idx"], item["type_id"], keys[0] if keys else item["valeur"])
        if sig not in seen:
            seen.add(sig)
            yield item


def _iter_raw(extraction: dict) -> Iterator[dict]:
    for i, dev in enumerate(extraction.get("dispositifs") or []):
        base = {"dispositif_idx": i, "nom_commercial": dev["nom_commercial"],
                "type_equipement": dev.get("type_equipement")}
        ids = dev["identifiants"]
        yield {**base, "type_id": "nom_commercial", "concerne": "direct", "valeur": dev["nom_commercial"]}
        for name in dev.get("noms_alternatifs") or []:
            yield {**base, "type_id": "nom_alternatif", "concerne": "direct", "valeur": name}
        for type_id in ID_TYPES:
            for value in ids.get(type_id) or []:
                yield {**base, "type_id": type_id, "concerne": "direct", "valeur": value}
        for p in ids.get("plages_numeriques") or []:
            yield {**base, "type_id": f"plage_{p['type']}", "concerne": "direct",
                   "valeur": f"{p['debut']} → {p['fin']}", "plage_debut": p["debut"], "plage_fin": p["fin"]}
        for prod in dev.get("produits_associes") or []:
            yield {**base, "type_id": "produit_associe_nom", "concerne": "associé",
                   "valeur": prod["nom"], "produit_associe_role": prod.get("role")}
            for ref in prod.get("references") or []:
                yield {**base, "type_id": "produit_associe_reference", "concerne": "associé",
                       "valeur": ref, "produit_associe_nom": prod["nom"], "produit_associe_role": prod.get("role")}


def reliability(type_id: str, value: str) -> str:
    """Confiance à accorder à une correspondance exacte sur cet identifiant seul.

    haute    : UDI/GTIN, Basic UDI-DI, plages, références/lots/séries d'au moins 5 caractères avec chiffres
    moyenne  : références courtes (4 car.), noms de plusieurs mots assez longs
    faible   : noms d'un seul mot, références de 3 caractères ou moins — à confirmer par fabricant/type
    contexte : versions logicielles, significatives seulement combinées au produit
    """
    if type_id in ("udi_di_gtin", "basic_udi_di") or type_id.startswith("plage_"):
        return "haute"
    if type_id == "versions_logicielles":
        return "contexte"
    if type_id in NAME_TYPES:
        w = words(value).split()
        return "moyenne" if len(w) >= 2 and len("".join(w)) >= 8 else "faible"
    key = compact(strip_prefix(type_id, value))
    if len(key) >= 5 and any(c.isdigit() for c in key):
        return "haute"
    return "moyenne" if len(key) >= 4 else "faible"


def identifier_keys(item: dict) -> list[str]:
    if item["type_id"].startswith("plage_"):
        kind = "lots" if item["type_id"] == "plage_lot" else "numeros_serie"
        return [normalize_identifier(kind, item["plage_debut"]), normalize_identifier(kind, item["plage_fin"])]
    return search_keys(item["type_id"], item["valeur"])
