"""Extraction locale du texte des PDF et détection déterministe des identifiants GS1/UDI.

La détection par regex sert de filet de sécurité : elle permet de vérifier que
les UDI-DI/GTIN présents dans la notice ont bien été repris par le LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pymupdf

# En dessous de ce nombre moyen de caractères par page, on considère le PDF comme scanné.
MIN_CHARS_PER_PAGE = 200


@dataclass
class PdfText:
    text: str
    page_count: int
    is_scanned: bool


def extract_text(pdf_bytes: bytes) -> PdfText:
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        pages = [p.get_text("text", sort=True) for p in doc]
    body = "\n".join(f"--- page {i + 1}/{len(pages)} ---\n{t.strip()}" for i, t in enumerate(pages))
    chars = sum(len(t.strip()) for t in pages)
    return PdfText(body, len(pages), is_scanned=chars < MIN_CHARS_PER_PAGE * max(len(pages), 1))


def gtin_is_valid(digits: str) -> bool:
    if not digits.isdigit() or len(digits) not in (8, 12, 13, 14):
        return False
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def normalize_gtin(digits: str) -> str:
    """GTIN-14 canonique (zéros à gauche), forme à utiliser pour comparer aux inventaires."""
    return digits.zfill(14)


# (01)04260521365002(10)LOT(21)SN — forme lisible des codes GS1 (HRI)
_GS1_AI_RE = re.compile(r"\((01|10|11|17|21|240)\)\s*([^\s(]+)")
# GTIN/EAN/UDI-DI suivis d'un nombre à 8-14 chiffres (espaces tolérés)
_KEYWORD_GTIN_RE = re.compile(
    r"(?:GTIN|EAN|UDI[\s-]*DI|\bDI\b|UDI)[^0-9\n]{0,25}((?:\d[  ]?){8,14})",
    re.IGNORECASE,
)


@dataclass
class DetectedIdentifiers:
    gtins: set[str] = field(default_factory=set)  # GTIN-14 normalisés, checksum valide
    lots: set[str] = field(default_factory=set)  # AI (10)
    serials: set[str] = field(default_factory=set)  # AI (21)

    def as_dict(self) -> dict[str, list[str]]:
        return {"gtin": sorted(self.gtins), "lot": sorted(self.lots), "numero_serie": sorted(self.serials)}


def detect_identifiers(text: str) -> DetectedIdentifiers:
    found = DetectedIdentifiers()
    for ai, value in _GS1_AI_RE.findall(text):
        value = value.strip(".,;:")
        if ai == "01":
            digits = re.sub(r"\D", "", value)
            if gtin_is_valid(digits):
                found.gtins.add(normalize_gtin(digits))
        elif ai == "10" and "*" not in value:
            found.lots.add(value)
        elif ai == "21" and "*" not in value:
            found.serials.add(value)
    for raw in _KEYWORD_GTIN_RE.findall(text):
        digits = re.sub(r"\D", "", raw)
        if gtin_is_valid(digits):
            found.gtins.add(normalize_gtin(digits))
    return found
