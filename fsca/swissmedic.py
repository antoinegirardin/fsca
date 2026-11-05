"""Client pour l'API publique (non documentée) de fsca.swissmedic.ch.

Endpoints découverts dans le bundle Angular du site :
  GET  /mep/api/publications?pageNumber=&sortingProperty=&direction=
  POST /mep/api/publications/search?pageNumber=&...   body: {queryTerm, fromDate, toDate, onlyUpdates}
  GET  /mep/api/publications/{swissmedicRef}/documents/{index}   -> PDF

Le tri "PUBLICATION_DATE" porte en réalité sur `statusDatum` (date de la dernière
modification), et le filtre fromDate/toDate aussi : une mise à jour d'un ancien
rappel réapparaît donc en tête de liste.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Iterator

import requests

BASE_URL = "https://fsca.swissmedic.ch/mep/api/publications"
LANG_PREFERENCE = ("FR", "EN", "DE", "IT")
USER_AGENT = "fsca-extractor/0.1 (+inventaire dispositifs medicaux)"

# "FR-2", "DE-1 - NaCl 0.9%", "DE-1-HCP", "EN-1-DIST"
_TITLE_RE = re.compile(r"^\s*([A-Za-z]{2})\s*-\s*(\d+)\s*(?:[-–]\s*)?(.*)$")


@dataclass(frozen=True)
class DocumentRef:
    index: int  # position dans la liste -> URL de téléchargement
    title: str
    language: str
    version: int
    variant: str  # "" si document unique, sinon p.ex. "HCP", "NaCl 0.9%"

    def url(self, ref: str) -> str:
        return f"{BASE_URL}/{ref}/documents/{self.index}"


def parse_documents(raw_docs: list[dict[str, Any]]) -> list[DocumentRef]:
    docs = []
    for i, d in enumerate(raw_docs):
        title = d.get("title") or ""
        m = _TITLE_RE.match(title)
        if m:
            lang, version, variant = m.group(1).upper(), int(m.group(2)), m.group(3).strip()
        else:
            lang, variant = (d.get("language") or "").upper(), title
            version = int(d["version"]) if str(d.get("version", "")).isdigit() else 0
        docs.append(DocumentRef(i, title, lang or (d.get("language") or "").upper(), version, variant))
    return docs


def select_documents(raw_docs: list[dict[str, Any]]) -> list[DocumentRef]:
    """Un document par variante : la version la plus récente, puis la langue préférée.

    La fraîcheur du contenu prime sur la langue : si seule la version DE-2 existe
    alors que FR s'arrête à FR-1, on prend DE-2 (la synthèse sera rédigée en français
    de toute façon).
    """
    by_variant: dict[str, list[DocumentRef]] = {}
    for d in parse_documents(raw_docs):
        by_variant.setdefault(d.variant.lower(), []).append(d)

    def lang_rank(d: DocumentRef) -> int:
        return LANG_PREFERENCE.index(d.language) if d.language in LANG_PREFERENCE else len(LANG_PREFERENCE)

    selected = []
    for docs in by_variant.values():
        latest = max(d.version for d in docs)
        candidates = [d for d in docs if d.version == latest]
        selected.append(min(candidates, key=lang_rank))
    return sorted(selected, key=lambda d: d.index)


class SwissmedicClient:
    def __init__(self, delay_s: float = 1.0, timeout_s: float = 60.0):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.delay_s = delay_s
        self.timeout_s = timeout_s
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = self.delay_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        for attempt in range(4):
            self._throttle()
            try:
                r = self.session.request(method, url, timeout=self.timeout_s, **kw)
                if r.status_code < 500 and r.status_code != 429:
                    r.raise_for_status()
                    return r
            except requests.ConnectionError:
                if attempt == 3:
                    raise
            time.sleep(2 ** attempt * 5)
        r.raise_for_status()
        return r

    def iter_publications(
        self, from_date: str | None = None, to_date: str | None = None, query: str | None = None
    ) -> Iterator[dict]:
        """Publications modifiées dans l'intervalle (dates ISO, bornes incluses), filtrables par mot-clé
        (la recherche plein texte accepte aussi une référence Swissmedic)."""
        page = 0
        params = {"sortingProperty": "PUBLICATION_DATE", "direction": "DESC"}
        criteria = {k: v for k, v in {"fromDate": from_date, "toDate": to_date, "queryTerm": query}.items() if v}
        while True:
            params["pageNumber"] = str(page)
            if criteria:
                data = self._request("POST", f"{BASE_URL}/search", params=params, json=criteria).json()
            else:
                data = self._request("GET", BASE_URL, params=params).json()
            yield from data["content"]
            page += 1
            if data.get("last", True) or page >= data.get("totalPages", 0):
                return

    def download(self, ref: str, doc: DocumentRef) -> tuple[bytes, str]:
        r = self._request("GET", doc.url(ref))
        return r.content, r.headers.get("Content-Type", "")
