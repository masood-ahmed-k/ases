#!/usr/bin/env python
"""Keep spec/requirements.yaml in sync with Appendix F of the ASES blueprint docx (ASES-DOC-02).

Default mode (--check, also the default with no flags): re-extracts Appendix F's table from the docx
and fails if its ID set doesn't exactly match requirements.yaml. This is what `swarm doctor` and CI
should run.

--regenerate: merges the current Appendix F into requirements.yaml -- adds new IDs as status
not_covered, drops IDs no longer in the docx (printing what it removed), and leaves the status/note of
every ID that's still present untouched. Meant to be run by a person after the docx changes, not by CI.

Usage:
    python spec/check_requirements.py --check                 (default)
    python spec/check_requirements.py --regenerate
    python spec/check_requirements.py --check --docx "path\\to\\blueprint.docx"
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import yaml
from docx import Document

DEFAULT_DOCX = pathlib.Path(r"C:\Users\masoo\OneDrive\Desktop\ASES_Swarm_Implementation_Blueprint_v1.2.docx")
REQUIREMENTS_YAML = pathlib.Path(__file__).resolve().parent / "requirements.yaml"


def extract_appendix_f(docx_path: pathlib.Path) -> list[dict]:
    if not docx_path.exists():
        raise FileNotFoundError(f"blueprint docx not found: {docx_path}")
    doc = Document(str(docx_path))

    # Find the "Appendix F" heading, then the first table that follows it in document order.
    body = doc.element.body
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    found_heading = False
    rows: list[dict] = []
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            para = Paragraph(child, doc)
            text = para.text.strip()
            style = para.style.name if para.style is not None else ""
            # Must be the real Heading 1 section, not the plain-text line with the same wording in the
            # Contents listing near the top of the document.
            if style == "Heading 1" and text.startswith("Appendix F"):
                found_heading = True
                continue
            if found_heading and style == "Heading 1" and text.startswith("Appendix G"):
                break
        elif child.tag == qn("w:tbl") and found_heading:
            table = Table(child, doc)
            for row in table.rows[1:]:  # skip header row
                cells = [c.text.strip() for c in row.cells]
                if len(cells) >= 4 and cells[0]:
                    rows.append(
                        {"id": cells[0], "requirement": cells[1], "section": cells[2], "verified_by": cells[3]}
                    )
            break
    if not found_heading:
        raise ValueError("could not find an 'Appendix F' heading in the docx")
    if not rows:
        raise ValueError("found 'Appendix F' but no table rows after it")
    return rows


def load_yaml_ids() -> dict[str, dict]:
    if not REQUIREMENTS_YAML.exists():
        return {}
    data = yaml.safe_load(REQUIREMENTS_YAML.read_text(encoding="utf-8")) or {}
    return {row["id"]: row for row in data.get("requirements", [])}


def cmd_check(docx_path: pathlib.Path) -> int:
    docx_rows = extract_appendix_f(docx_path)
    docx_ids = {r["id"] for r in docx_rows}
    yaml_rows = load_yaml_ids()
    yaml_ids = set(yaml_rows)

    missing_from_yaml = sorted(docx_ids - yaml_ids)
    stale_in_yaml = sorted(yaml_ids - docx_ids)

    if not missing_from_yaml and not stale_in_yaml:
        print(f"OK: {len(docx_ids)} requirement IDs in sync between {docx_path.name} and requirements.yaml")
        return 0

    print(f"DRIFT between {docx_path.name} (Appendix F) and {REQUIREMENTS_YAML.name}:")
    if missing_from_yaml:
        print(f"  in docx but not in requirements.yaml ({len(missing_from_yaml)}): {missing_from_yaml}")
    if stale_in_yaml:
        print(f"  in requirements.yaml but not in docx ({len(stale_in_yaml)}): {stale_in_yaml}")
    print("Run `python spec/check_requirements.py --regenerate` to fix, then review the diff.")
    return 1


def cmd_regenerate(docx_path: pathlib.Path) -> int:
    docx_rows = extract_appendix_f(docx_path)
    existing = load_yaml_ids()

    merged = []
    for row in docx_rows:
        prior = existing.get(row["id"], {})
        merged.append({
            "id": row["id"],
            "requirement": row["requirement"],
            "section": row["section"],
            "verified_by": row["verified_by"],
            "status": prior.get("status", "not_covered"),
            "note": prior.get("note", ""),
        })

    docx_ids = {r["id"] for r in docx_rows}
    removed = sorted(set(existing) - docx_ids)
    added = sorted(docx_ids - set(existing))

    REQUIREMENTS_YAML.write_text(
        yaml.safe_dump(
            {"source_docx": docx_path.name, "requirements": merged},
            sort_keys=False, allow_unicode=True, width=100,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {len(merged)} requirements to {REQUIREMENTS_YAML}")
    if added:
        print(f"  added ({len(added)}): {added}")
    if removed:
        print(f"  removed ({len(removed)}): {removed}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Fail on drift (default).")
    mode.add_argument("--regenerate", action="store_true", help="Merge Appendix F into requirements.yaml.")
    parser.add_argument("--docx", type=pathlib.Path, default=DEFAULT_DOCX, help="Path to the blueprint docx.")
    args = parser.parse_args(argv)

    try:
        if args.regenerate:
            return cmd_regenerate(args.docx)
        return cmd_check(args.docx)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
