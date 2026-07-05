#!/usr/bin/env python3
"""Self-contained smoke test for the Hermes Report Deck backend.

Runs the FastAPI router against the bundled sample-reports/ directory and
asserts the read-only API behaves and stays inside the report root.

Usage:
    pip install fastapi httpx   # PyYAML optional
    python tests/smoke_test.py
"""

import importlib
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
DASHBOARD = REPO / "dashboard"
SAMPLES = REPO / "sample-reports"

PASSED = 0


def ok(cond, msg):
    global PASSED
    print(("PASS" if cond else "FAIL"), msg)
    if not cond:
        raise AssertionError(msg)
    PASSED += 1


def main() -> int:
    # A file OUTSIDE the report root that must never be readable via the API.
    outside_dir = tempfile.mkdtemp(prefix="report-deck-outside-")
    secret = pathlib.Path(outside_dir) / "secret.md"
    secret.write_text("# SECRET\nshould never be served\n", encoding="utf-8")

    os.environ["REPORT_DECK_ROOT"] = str(SAMPLES)
    sys.path.insert(0, str(DASHBOARD))

    import plugin_api  # noqa: E402
    importlib.reload(plugin_api)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/hermes-report-deck")
    c = TestClient(app)
    B = "/api/plugins/hermes-report-deck"

    # --- health ---
    hz = c.get(B + "/health").json()
    ok(hz["ok"] and hz["exists"], "health: root exists")
    ok(hz["count"] >= 4, "health: counts sample reports")

    # --- list ---
    listing = c.get(B + "/reports").json()
    reports = listing["reports"]
    ok(listing["count"] >= 4, "list: sample reports present")
    ok(all("markdown" not in r and "content" not in r for r in reports), "list: no body fields leak")
    ok("automation" in listing["facets"]["lanes"], "list: lane facet present")
    ok("crawl" in listing["facets"]["tags"], "list: tag facet present")
    # newest first
    dates = [r["generated_at"] for r in reports if r["generated_at"]]
    ok(dates == sorted(dates, reverse=True), "list: sorted newest first")
    # frontmatter-less file still listed, title from first heading
    plain = [r for r in reports if r["id"] == "plain-note"]
    ok(plain and plain[0]["title"] == "Plain note without frontmatter", "list: frontmatter-optional file listed")

    # --- read one ---
    detail = c.get(B + "/reports/nightly-crawl-2026-06-01").json()
    ok(detail["report"]["title"] == "Nightly crawl summary", "read: metadata")
    ok("Nightly crawl summary" in detail["markdown"], "read: markdown body")
    ok("---" not in detail["markdown"].splitlines()[0:1], "read: frontmatter stripped from body")
    ok(detail["rendering_contract"]["raw_html_allowed"] is False, "read: rendering contract locked down")

    # --- filters ---
    ok(c.get(B + "/reports?lane=automation").json()["count"] == 1, "filter: lane")
    ok(c.get(B + "/reports?source=ci").json()["count"] == 1, "filter: source")
    ok(c.get(B + "/reports?tag=weekly").json()["count"] == 1, "filter: tag")
    ok(c.get(B + "/reports?date=2026-06").json()["count"] == 2, "filter: date prefix")
    ok(c.get(B + "/reports?lane=nope").json()["count"] == 0, "filter: no match")
    ok(c.get(B + "/reports?lane=%40bad").status_code == 400, "filter: invalid value rejected")
    ok(c.get(B + "/reports?date=notadate").status_code == 400, "filter: invalid date rejected")

    # --- missing + traversal ---
    ok(c.get(B + "/reports/does-not-exist").status_code == 404, "missing report 404")
    ok(c.get(B + "/reports/..%2f..%2fsecret").status_code == 404, "encoded traversal rejected")
    ok(c.get(B + "/reports/%2e%2e%2fsecret").status_code == 404, "dotdot id rejected")

    # --- never reads outside the root ---
    # The secret file lives outside REPORT_DECK_ROOT; no id can reach it.
    all_ids = [r["id"] for r in c.get(B + "/reports").json()["reports"]]
    ok(not any("secret" in i for i in all_ids), "no outside file discovered")
    for probe in ("secret", "secret.md", outside_dir + "/secret"):
        ok(c.get(B + "/reports/" + probe).status_code == 404, f"outside file not served ({probe!r})")

    # --- fallback frontmatter parser (covers the no-PyYAML path directly) ---
    mini = plugin_api._mini_yaml(
        "title: Hello\nlane: ops\ntags: [a, b]\nrelated:\n  - one\n  - two\nsummary: \"q\"\n"
    )
    ok(mini["title"] == "Hello" and mini["lane"] == "ops", "mini-yaml: key/value")
    ok(mini["tags"] == ["a", "b"], "mini-yaml: inline list")
    ok(mini["related"] == ["one", "two"], "mini-yaml: block list")
    ok(mini["summary"] == "q", "mini-yaml: quoted scalar")

    print(f"\nALL {PASSED} SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
