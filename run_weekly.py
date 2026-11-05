#!/usr/bin/env python3
"""Extraction hebdomadaire des avis de sécurité publiés sur fsca.swissmedic.ch.

Exemples :
  python run_weekly.py                      # depuis le dernier passage réussi (ou 7 jours)
  python run_weekly.py --dry-run --limit 5  # sans appel à Claude : téléchargement + texte + regex
  python run_weekly.py --from 2026-10-01 --to 2026-10-31
  python run_weekly.py --ref Vk_20261104_023 --force
  python run_weekly.py --export-all         # réexporte toute la base sans rien télécharger
  python run_weekly.py --publish            # passage puis commit + push de data/exports/complet
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from fsca.export import export
from fsca.schema import to_record
from fsca.pdftext import detect_identifiers, extract_text, normalize_gtin
from fsca.keys import normalize_identifier
from fsca.store import Store
from fsca.swissmedic import SwissmedicClient, select_documents

log = logging.getLogger("fsca")

OVERLAP_DAYS = 2  # recouvrement entre deux passages : une publication tardive n'est pas manquée


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="from_date", help="date de début AAAA-MM-JJ (date de dernière modification)")
    p.add_argument("--to", dest="to_date", help="date de fin AAAA-MM-JJ (défaut : aujourd'hui)")
    p.add_argument("--ref", action="append", help="traiter uniquement cette référence (répétable)")
    p.add_argument("--limit", type=int, help="nombre maximal de publications à traiter")
    p.add_argument("--dry-run", action="store_true", help="ne pas appeler Claude")
    p.add_argument("--force", action="store_true", help="ré-extraire même si les documents n'ont pas changé")
    p.add_argument("--model", default=None, help="modèle Claude (défaut : claude-opus-5-5)")
    p.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    p.add_argument("--export-all", action="store_true", help="exporter toute la base puis quitter")
    p.add_argument("--publish", action="store_true",
                   help="après le passage, commit + push de l'export complet (data/exports/complet)")
    return p.parse_args()


def setup_logging(data_dir: Path) -> None:
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = [logging.StreamHandler(sys.stdout),
                logging.FileHandler(data_dir / "logs" / f"{date.today().isoformat()}.log", encoding="utf-8")]
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


def safe_filename(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name).strip("_")


def cross_check(extraction: dict, regex: dict) -> dict:
    """Compare les GTIN détectés par regex à ceux extraits par Claude."""
    extracted = {
        normalize_identifier("udi_di_gtin", v)
        for dev in extraction["dispositifs"]
        for v in dev["identifiants"]["udi_di_gtin"]
    }
    missing = sorted(g for g in regex["gtin"] if normalize_gtin(g) not in extracted)
    return {"gtin_absents_extraction": missing}


def publish(paths: list[Path], message: str) -> bool:
    """git add + commit + push des exports publiés. Utilise le dépôt et le remote configurés."""
    repo = Path(__file__).resolve().parent

    def git(*a: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *a], cwd=repo, check=check, capture_output=True, text=True)

    try:
        git("add", "--all", "--", *[str(p) for p in paths])
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            log.info("Publication : aucun changement à pousser")
            return True
        git("commit", "-m", message)
        git("push")
        log.info("Publication : exports poussés (%s)", message)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log.error("Publication git échouée : %s", getattr(e, "stderr", None) or e)
        return False


def main() -> int:
    args = parse_args()
    setup_logging(args.data_dir)
    store = Store(args.data_dir / "fsca.sqlite")

    complet = args.data_dir / "exports" / "complet"
    if args.export_all:
        export(store.all_rows(), complet)
        log.info("Export complet : %s", complet)
        if args.publish and not publish([complet], f"Exports FSCA {date.today().isoformat()}"):
            return 3
        return 0

    to_date = args.to_date or date.today().isoformat()
    from_date = args.from_date
    if not from_date and not args.ref:
        last = store.last_successful_to_date()
        start = date.fromisoformat(last) - timedelta(days=OVERLAP_DAYS) if last else date.today() - timedelta(days=7)
        from_date = start.isoformat()

    client_claude = None
    if not args.dry_run:
        import anthropic

        from fsca.extract import DEFAULT_EFFORT, DEFAULT_MODEL, ExtractionError, LoadedDocument, extract_notice

        client_claude = anthropic.Anthropic()
        model, effort = args.model or DEFAULT_MODEL, args.effort or DEFAULT_EFFORT

    sm = SwissmedicClient()
    if args.ref:
        publications = (p for r in args.ref for p in sm.iter_publications(query=r) if p["swissmedicRef"] == r)
        log.info("Recherche de %s", ", ".join(args.ref))
    else:
        publications = sm.iter_publications(from_date, to_date)
        log.info("Publications modifiées du %s au %s", from_date, to_date)

    # Un passage partiel (--ref, --limit atteint) ne sert pas de point de départ au suivant.
    run_id = store.start_run(from_date, None if args.ref else to_date, args.dry_run)
    complete = not args.ref
    touched: list[str] = []
    n_seen = n_extracted = n_skipped = n_errors = 0
    total_in = total_out = 0

    for pub in publications:
        ref = pub["swissmedicRef"]
        n_seen += 1
        selected = select_documents(pub.get("documents") or [])
        docs_key = "|".join(d.title for d in selected)
        if not args.force and not store.needs_processing(ref, docs_key, want_extraction=not args.dry_run):
            n_skipped += 1
            log.info("%s : inchangé, ignoré", ref)
            continue
        if args.limit and len(touched) >= args.limit:
            complete = False
            break
        touched.append(ref)
        docs_meta = [{"title": d.title, "language": d.language, "version": d.version, "variant": d.variant,
                      "url": d.url(ref)} for d in selected]
        log.info("%s : %s – documents %s", ref, pub.get("hersteller"), docs_key or "(aucun)")

        try:
            if not selected:
                raise RuntimeError("aucun document publié")
            loaded = []
            for d in selected:
                pdf, ctype = sm.download(ref, d)
                pdf_dir = args.data_dir / "pdfs" / ref
                pdf_dir.mkdir(parents=True, exist_ok=True)
                (pdf_dir / f"{safe_filename(d.title)}.pdf").write_bytes(pdf)
                if "pdf" not in ctype.lower() and not pdf.startswith(b"%PDF"):
                    raise RuntimeError(f"document {d.title} n'est pas un PDF ({ctype})")
                text = extract_text(pdf)
                if text.is_scanned:
                    log.info("%s : %s semble scanné (%d pages), envoyé en PDF à Claude", ref, d.title,
                             text.page_count)
                loaded.append((d, pdf, text))

            full_text = "\n\n".join(f"===== {d.title} =====\n{t.text}" for d, _, t in loaded)
            regex = detect_identifiers(full_text).as_dict()

            if args.dry_run:
                store.save(pub, docs_key, docs_meta, full_text, regex)
                log.info("%s : %d car., GTIN détectés %s", ref, len(full_text), regex["gtin"] or "aucun")
                continue

            result = extract_notice(
                client_claude, pub, [LoadedDocument(d, pdf, t) for d, pdf, t in loaded], model=model, effort=effort
            )
            extraction = to_record(result.extraction)
            controles = cross_check(extraction, regex)
            if controles["gtin_absents_extraction"]:
                log.warning("%s : GTIN détectés mais non extraits : %s", ref, controles["gtin_absents_extraction"])
            store.save(pub, docs_key, docs_meta, full_text, regex, extraction, controles,
                       result.model, result.input_tokens, result.output_tokens)
            n_extracted += 1
            total_in += result.input_tokens
            total_out += result.output_tokens
            ev = extraction["evaluation"]
            log.info("%s : OK – %d dispositif(s), urgence %s, classe %s (%d→%d tokens)", ref,
                     len(extraction["dispositifs"]), ev["urgence"], ev["classe_rappel_estimee"],
                     result.input_tokens, result.output_tokens)
        except Exception as e:  # une notice en échec ne doit pas bloquer les autres
            if client_claude is not None and not isinstance(e, (ExtractionError, RuntimeError)):
                import anthropic

                if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
                    log.error("Authentification Claude refusée : %s", e)
                    store.finish_run(run_id, n_seen, n_extracted, n_skipped, n_errors + 1, complete=False)
                    return 2
            n_errors += 1
            log.error("%s : échec – %s: %s", ref, type(e).__name__, e)
            store.save(pub, docs_key, docs_meta, None, None, error=f"{type(e).__name__}: {e}")

    store.finish_run(run_id, n_seen, n_extracted, n_skipped, n_errors, complete)
    out_dir = args.data_dir / "exports" / date.today().isoformat()
    if touched:
        export(store.rows_for(touched), out_dir)
        log.info("Exports du passage : %s", out_dir)
    # Vue cumulée de toute la base, régénérée à chaque passage : c'est elle qui est publiée.
    export(store.all_rows(), complet)
    log.info("Export complet : %s", complet)
    log.info("Terminé : %d vues, %d extraites, %d inchangées, %d erreurs ; tokens %d entrée / %d sortie",
             n_seen, n_extracted, n_skipped, n_errors, total_in, total_out)
    store.close()
    if args.publish and not publish(
        [complet], f"Exports FSCA {to_date} ({n_extracted} notice(s) extraite(s), {n_errors} erreur(s))"
    ):
        return 3
    return 1 if n_errors else 0


if __name__ == "__main__":
    sys.exit(main())
