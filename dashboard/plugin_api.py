"""Hermes Report Deck — Markdown report browser (backend).

Mounted by the Hermes Dashboard at /api/plugins/hermes-report-deck/ when
installed. It reads Markdown reports (with optional YAML frontmatter) from a
single allow-listed report root and exposes report-read routes plus a small
folder-visibility configuration route:

- GET /health
- GET /folders             (list top-level folders + visibility state)
- PUT /folders             (save folder visibility config outside report root)
- GET /reports             (list metadata, with facets + filters)
- GET /reports/{report_id} (one report's metadata + Markdown body)

There are intentionally no report write/delete/edit routes, no external network
calls, and no arbitrary file access: the backend only ever reads Markdown files
it has itself discovered under the configured report root. The only write is a
small JSON folder-visibility preference at REPORT_DECK_CONFIG, outside the
report root.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter()

# --------------------------------------------------------------------------- #
# Report root (allow-listed, env-overridable)
# --------------------------------------------------------------------------- #

_HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
_DEFAULT_ROOT = str(_HERMES_HOME / "reports")
REPORT_ROOT = Path(os.environ.get("REPORT_DECK_ROOT", _DEFAULT_ROOT)).resolve()
INDEX_PATH = REPORT_ROOT / "index.json"

# Folder-visibility config. Stored OUTSIDE the report root (which stays
# read-only) — default under $HERMES_HOME; override with REPORT_DECK_CONFIG.
CONFIG_PATH = Path(os.environ.get("REPORT_DECK_CONFIG", str(_HERMES_HOME / "report-deck.json")))

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


def _safe_index_content_path(raw: Any) -> Optional[Path]:
    """Resolve an index.json content_path and keep it inside REPORT_ROOT."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    decoded = unquote(raw.strip())
    p = Path(decoded)
    if p.is_absolute() or "\x00" in decoded or any(part == ".." for part in p.parts):
        return None
    resolved = (REPORT_ROOT / p).resolve()
    try:
        resolved.relative_to(REPORT_ROOT)
    except ValueError:
        return None
    if resolved.suffix.lower() not in _MD_SUFFIXES or not resolved.is_file():
        return None
    return resolved


def _load_index_records() -> Optional[List[Dict[str, Any]]]:
    """Prefer optional index.json metadata when present.

    This preserves stable IDs, timestamps, source/lane/tag facets, summaries,
    related IDs, and explicit content_path values for generated report shelves.
    Roots without a valid index fall back to recursive Markdown discovery.
    """
    if not INDEX_PATH.is_file():
        return None
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    reports = data.get("reports") if isinstance(data, dict) else None
    if not isinstance(reports, list):
        return None

    records: List[Dict[str, Any]] = []
    seen_ids: Dict[str, int] = {}
    for item in reports:
        if not isinstance(item, dict):
            continue
        path = _safe_index_content_path(item.get("content_path"))
        if path is None:
            continue
        rid = item.get("id")
        if not (isinstance(rid, str) and _ID_RE.match(rid)):
            rid = _slug_id(path.relative_to(REPORT_ROOT).as_posix())
        if rid in seen_ids:
            seen_ids[rid] += 1
            rid = f"{rid}-{seen_ids[rid]}"
        else:
            seen_ids[rid] = 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        _meta, body = _split_frontmatter(text)
        records.append({
            "id": rid,
            "title": str(item.get("title") or _first_heading(body) or path.stem),
            "generated_at": str(item.get("generated_at") or item.get("date") or ""),
            "source": str(item.get("source") or ""),
            "lane": str(item.get("lane") or ""),
            "tags": _tags_of(item),
            "summary": str(item.get("summary") or ""),
            "related": _related_of(item),
            "_path": path,
            "_body": body,
            "_rel": path.relative_to(REPORT_ROOT).as_posix(),
        })
    return records


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
    indexed = _load_index_records()
    if indexed is not None:
        return indexed

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
# Folder visibility config
# --------------------------------------------------------------------------- #


def _load_cfg() -> Dict[str, Any]:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    mode = data.get("folders_mode")
    folders = data.get("visible_folders")
    clean: List[str] = []
    if isinstance(folders, list):
        for f in folders:
            s = str(f).strip()
            if "/" in s or "\\" in s or ".." in s or len(s) > 120:
                continue
            if s not in clean:
                clean.append(s)
    return {
        "folders_mode": mode if mode in ("all", "selected") else "all",
        "visible_folders": clean,
    }


def _save_cfg(cfg: Dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def _top_folder(rel: str) -> str:
    """Top-level folder of a report's relative path ("" = report root)."""
    return rel.split("/", 1)[0] if "/" in rel else ""


def _folder_visible(cfg: Dict[str, Any], top: str) -> bool:
    if cfg["folders_mode"] == "all":
        return True
    return top in set(cfg["visible_folders"])


def _visible_records(cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    cfg = cfg or _load_cfg()
    return [r for r in _build_records() if _folder_visible(cfg, _top_folder(r["_rel"]))]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/health")
def health() -> Dict[str, Any]:
    exists = REPORT_ROOT.is_dir()
    cfg = _load_cfg()
    return {
        "ok": True,
        "report_root": str(REPORT_ROOT),
        "exists": exists,
        "count": len(_visible_records(cfg)) if exists else 0,
        "folders_mode": cfg["folders_mode"],
    }


class FoldersIn(BaseModel):
    mode: str = "all"
    folders: List[str] = Field(default_factory=list)


def _folders_state() -> Dict[str, Any]:
    cfg = _load_cfg()
    counts: Dict[str, int] = {}
    for r in _build_records():
        top = _top_folder(r["_rel"])
        counts[top] = counts.get(top, 0) + 1
    folders = [
        {"name": name, "label": name or "(root)", "count": counts[name], "visible": _folder_visible(cfg, name)}
        for name in sorted(counts.keys())
    ]
    return {
        "schema_version": 1,
        "mode": cfg["folders_mode"],
        "visible_folders": cfg["visible_folders"],
        "folders": folders,
    }


@router.get("/folders")
def list_folders() -> Dict[str, Any]:
    """List top-level folders under the report root and which are visible."""
    return _folders_state()


@router.put("/folders")
def set_folders(body: FoldersIn) -> Dict[str, Any]:
    """Configure which top-level folders show up (mode 'all' or 'selected')."""
    if body.mode not in ("all", "selected"):
        raise _bad_request("mode must be 'all' or 'selected'")
    clean: List[str] = []
    for f in body.folders:
        s = str(f).strip()
        if "/" in s or "\\" in s or ".." in s or len(s) > 120:
            continue  # single path segments only ("" = report root)
        if s not in clean:
            clean.append(s)
    _save_cfg({"folders_mode": body.mode, "visible_folders": clean})
    return _folders_state()


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

    records = _visible_records()
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

    for record in _visible_records():
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
