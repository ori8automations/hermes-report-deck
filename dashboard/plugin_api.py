"""Hermes Report Deck — read-only Markdown report browser (backend).

Mounted by the Hermes Dashboard at /api/plugins/hermes-report-deck/ when
installed. It reads Markdown reports (with optional YAML frontmatter) from a
single allow-listed report root and exposes read-only routes:

- GET /health
- GET /reports            (list metadata, with facets + filters)
- GET /reports/{report_id}  (one report's metadata + Markdown body)

There are intentionally no write/delete/edit routes, no external network calls,
and no arbitrary file access: the backend only ever reads Markdown files it has
itself discovered under the configured report root.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

# --------------------------------------------------------------------------- #
# Report root (allow-listed, env-overridable)
# --------------------------------------------------------------------------- #

_HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
_DEFAULT_ROOT = str(_HERMES_HOME / "reports")
REPORT_ROOT = Path(os.environ.get("REPORT_DECK_ROOT", _DEFAULT_ROOT)).resolve()

_MD_SUFFIXES = {".md", ".markdown"}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_FILTER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\- ]{0,63}$")
_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")

# Metadata keys surfaced to the UI. Body-like keys are never returned in list
# output (defense in depth — frontmatter should not smuggle a body into a list).
_PUBLIC_KEYS = ("id", "title", "generated_at", "source", "lane", "tags", "summary", "related")
_BODYLIKE_KEYS = {"content", "markdown", "body", "raw", "text", "content_path"}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="report not found")


# --------------------------------------------------------------------------- #
# Frontmatter parsing (PyYAML if available, tiny fallback otherwise)
# --------------------------------------------------------------------------- #


def _scalar(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _mini_yaml(block: str) -> Dict[str, Any]:
    """Best-effort frontmatter parser for the common key/value + list cases.

    Only used when PyYAML is not importable. Handles ``key: value``, inline
    ``key: [a, b]``, and block lists (``key:`` followed by ``- item`` lines).
    """
    data: Dict[str, Any] = {}
    cur_key: Optional[str] = None
    for raw in block.split("\n"):
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        item = re.match(r"^\s*-\s+(.*)$", raw)
        if item and cur_key is not None:
            if not isinstance(data.get(cur_key), list):
                data[cur_key] = []
            data[cur_key].append(_scalar(item.group(1)))
            continue
        kv = re.match(r"^([A-Za-z0-9_\-]+):\s*(.*)$", raw)
        if kv:
            key, val = kv.group(1), kv.group(2).strip()
            if val == "":
                data[key] = ""      # may be filled by following block list
                cur_key = key
            elif val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                data[key] = [_scalar(x) for x in inner.split(",") if x.strip()] if inner else []
                cur_key = None
            else:
                data[key] = _scalar(val)
                cur_key = None
    return data


def _parse_frontmatter_block(block: str) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(block)
        return data if isinstance(data, dict) else {}
    except Exception:
        return _mini_yaml(block)


def _split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Return (metadata, body). Metadata is {} when no frontmatter is present."""
    m = re.match(r"^﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    meta = _parse_frontmatter_block(m.group(1))
    return (meta if isinstance(meta, dict) else {}), m.group(2)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def _slug_id(rel_path: str) -> str:
    stem = re.sub(r"\.(md|markdown)$", "", rel_path, flags=re.I)
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return slug[:128] or "report"


def _first_heading(body: str) -> str:
    for line in body.split("\n"):
        m = re.match(r"^#{1,6}\s+(.+)$", line.strip())
        if m:
            return m.group(1).strip()
    return ""


def _iter_report_files() -> List[Path]:
    """All Markdown files under REPORT_ROOT, resolved and confined to the root."""
    if not REPORT_ROOT.is_dir():
        return []
    files: List[Path] = []
    for p in REPORT_ROOT.rglob("*"):
        if p.suffix.lower() not in _MD_SUFFIXES:
            continue
        try:
            resolved = p.resolve()
            resolved.relative_to(REPORT_ROOT)  # reject symlink escapes
        except (ValueError, OSError):
            continue
        if resolved.is_file():
            files.append(resolved)
    return sorted(files, key=lambda x: x.as_posix())


def _tags_of(meta: Dict[str, Any]) -> List[str]:
    value = meta.get("tags")
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    if isinstance(value, str) and value.strip():
        return [t.strip() for t in value.split(",") if t.strip()]
    return []


def _related_of(meta: Dict[str, Any]) -> List[str]:
    value = meta.get("related")
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    return []


def _build_records() -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    seen_ids: Dict[str, int] = {}
    for path in _iter_report_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta, body = _split_frontmatter(text)
        rel = path.relative_to(REPORT_ROOT).as_posix()

        rid = meta.get("id")
        if not (isinstance(rid, str) and _ID_RE.match(rid)):
            rid = _slug_id(rel)
        if rid in seen_ids:  # ensure uniqueness deterministically
            seen_ids[rid] += 1
            rid = f"{rid}-{seen_ids[rid]}"
        else:
            seen_ids[rid] = 1

        generated_at = meta.get("generated_at") or meta.get("date") or ""
        records.append(
            {
                "id": rid,
                "title": str(meta.get("title") or _first_heading(body) or Path(rel).stem),
                "generated_at": str(generated_at),
                "source": str(meta.get("source") or ""),
                "lane": str(meta.get("lane") or ""),
                "tags": _tags_of(meta),
                "summary": str(meta.get("summary") or ""),
                "related": _related_of(meta),
                "_path": path,
                "_body": body,
                "_rel": rel,
            }
        )
    return records


def _public(record: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: record.get(k) for k in _PUBLIC_KEYS}
    for forbidden in _BODYLIKE_KEYS:
        out.pop(forbidden, None)
    return out


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #


def _clean_filter(value: Optional[str], name: str) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if not _FILTER_RE.match(value):
        raise _bad_request(f"invalid {name} filter")
    return value.lower()


def _clean_date(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if not _DATE_RE.match(value):
        raise _bad_request("invalid date filter (use YYYY, YYYY-MM, or YYYY-MM-DD)")
    return value


def _matches(record: Dict[str, Any], lane, source, tag, date) -> bool:
    if lane and record["lane"].lower() != lane:
        return False
    if source and record["source"].lower() != source:
        return False
    if tag and tag not in [t.lower() for t in record["tags"]]:
        return False
    if date and not str(record["generated_at"]).startswith(date):
        return False
    return True


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/health")
def health() -> Dict[str, Any]:
    exists = REPORT_ROOT.is_dir()
    return {
        "ok": True,
        "report_root": str(REPORT_ROOT),
        "exists": exists,
        "count": len(_iter_report_files()) if exists else 0,
    }


@router.get("/reports")
def list_reports(
    lane: Optional[str] = Query(None, description="Exact lane filter"),
    source: Optional[str] = Query(None, description="Exact source filter"),
    tag: Optional[str] = Query(None, description="Exact tag filter"),
    date: Optional[str] = Query(None, description="Date prefix filter: YYYY, YYYY-MM, or YYYY-MM-DD"),
) -> Dict[str, Any]:
    """List report metadata only, newest generated report first."""
    lane_f = _clean_filter(lane, "lane")
    source_f = _clean_filter(source, "source")
    tag_f = _clean_filter(tag, "tag")
    date_f = _clean_date(date)

    records = _build_records()
    facets = {
        "lanes": sorted({r["lane"] for r in records if r["lane"]}),
        "sources": sorted({r["source"] for r in records if r["source"]}),
        "tags": sorted({t for r in records for t in r["tags"]}),
    }
    matched = [r for r in records if _matches(r, lane_f, source_f, tag_f, date_f)]
    matched.sort(key=lambda r: str(r.get("generated_at") or ""), reverse=True)

    return {
        "schema_version": 1,
        "report_root": str(REPORT_ROOT),
        "count": len(matched),
        "filters": {"lane": lane_f, "source": source_f, "tag": tag_f, "date": date_f},
        "facets": facets,
        "reports": [_public(r) for r in matched],
    }


@router.get("/reports/{report_id}")
def get_report(report_id: str) -> Dict[str, Any]:
    """Return one report's metadata and Markdown body (frontmatter stripped)."""
    decoded = unquote(str(report_id or "").strip())
    # IDs are identifiers, never paths. Reject traversal-looking IDs outright;
    # regardless, lookup is by matching discovered ids, so no path is ever
    # constructed from user input.
    if not _ID_RE.match(decoded) or "/" in decoded or "\\" in decoded or ".." in decoded:
        raise _not_found()

    for record in _build_records():
        if record["id"] == decoded:
            return {
                "schema_version": 1,
                "report_root": str(REPORT_ROOT),
                "report": _public(record),
                "markdown": record["_body"],
                "rendering_contract": {
                    "raw_html_allowed": False,
                    "active_content_allowed": False,
                    "renderer": "client-react-text-nodes",
                },
            }
    raise _not_found()
