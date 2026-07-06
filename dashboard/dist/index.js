/*
 * Hermes Report Deck — Markdown report browser (frontend).
 *
 * No build step: a plain IIFE that renders with the React instance provided by
 * the Hermes Plugin SDK. Markdown is rendered into React text nodes only — no
 * raw HTML, no active content, and links are restricted to http(s)/mailto.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  var plugins = window.__HERMES_PLUGINS__;
  if (!SDK || !plugins || !SDK.React) return;

  var React = SDK.React;
  var h = React.createElement;
  var hooks = SDK.hooks || React;
  var useEffect = hooks.useEffect;
  var useMemo = hooks.useMemo;
  var useState = hooks.useState;

  var API = "/api/plugins/hermes-report-deck";

  function fetchJSON(path, opts) {
    return fetch(API + path, Object.assign({ credentials: "same-origin" }, opts || {})).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (body) { throw new Error(res.status + ": " + body); });
      }
      return res.json();
    });
  }

  // ---- safe inline + block markdown → React nodes -------------------------- //

  function safeUrl(url) {
    if (!url || typeof url !== "string") return null;
    try {
      var parsed = new URL(url, window.location.origin);
      if (parsed.protocol === "http:" || parsed.protocol === "https:" || parsed.protocol === "mailto:") {
        return parsed.href;
      }
    } catch (_e) { /* ignore */ }
    return null;
  }

  function inlineNodes(text, keyPrefix) {
    var out = [];
    var re = /(`([^`]+)`)|\[([^\]]+)\]\(([^)\s]+)\)|(\*\*([^*]+)\*\*)|(\*([^*]+)\*)/g;
    var last = 0, m, i = 0;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) out.push(text.slice(last, m.index));
      if (m[2]) {
        out.push(h("code", { key: keyPrefix + "-code-" + i++ }, m[2]));
      } else if (m[3]) {
        var href = safeUrl(m[4]);
        out.push(h("a", {
          key: keyPrefix + "-link-" + i++, href: href || undefined,
          target: href ? "_blank" : undefined, rel: href ? "noreferrer noopener" : undefined,
          className: href ? undefined : "hrd-unsafe-link"
        }, m[3]));
      } else if (m[6]) {
        out.push(h("strong", { key: keyPrefix + "-strong-" + i++ }, m[6]));
      } else if (m[8]) {
        out.push(h("em", { key: keyPrefix + "-em-" + i++ }, m[8]));
      }
      last = re.lastIndex;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }

  function markdownBlocks(markdown) {
    var lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
    var blocks = [], para = [], list = [], code = [], inCode = false;

    function flushPara() {
      if (para.length) {
        var text = para.join(" ").trim();
        blocks.push(h("p", { key: "p-" + blocks.length }, inlineNodes(text, "p-" + blocks.length)));
        para = [];
      }
    }
    function flushList() {
      if (list.length) {
        blocks.push(h("ul", { key: "ul-" + blocks.length }, list.map(function (item, idx) {
          return h("li", { key: idx }, inlineNodes(item, "li-" + blocks.length + "-" + idx));
        })));
        list = [];
      }
    }

    lines.forEach(function (line) {
      if (line.trim().indexOf("```") === 0) {
        if (inCode) {
          blocks.push(h("pre", { key: "pre-" + blocks.length }, h("code", null, code.join("\n"))));
          code = []; inCode = false;
        } else { flushPara(); flushList(); inCode = true; }
        return;
      }
      if (inCode) { code.push(line); return; }
      if (!line.trim()) { flushPara(); flushList(); return; }
      var heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        flushPara(); flushList();
        var tag = "h" + heading[1].length;
        blocks.push(h(tag, { key: tag + "-" + blocks.length }, inlineNodes(heading[2], tag + "-" + blocks.length)));
        return;
      }
      var bullet = line.match(/^\s*[-*]\s+(.+)$/);
      if (bullet) { flushPara(); list.push(bullet[1]); return; }
      flushList();
      para.push(line.trim());
    });
    if (inCode) blocks.push(h("pre", { key: "pre-" + blocks.length }, h("code", null, code.join("\n"))));
    flushPara(); flushList();
    return blocks;
  }

  // ---- component ----------------------------------------------------------- //

  function ReportDeck() {
    var rs = useState([]); var reports = rs[0], setReports = rs[1];
    var fs = useState({ lanes: [], sources: [], tags: [] }); var facets = fs[0], setFacets = fs[1];
    var ff = useState({ lane: "", source: "", tag: "", date: "" }); var filters = ff[0], setFilters = ff[1];
    var sel = useState(null); var selectedId = sel[0], setSelectedId = sel[1];
    var dt = useState(null); var detail = dt[0], setDetail = dt[1];
    var ld = useState(true); var loading = ld[0], setLoading = ld[1];
    var er = useState(null); var error = er[0], setError = er[1];
    var fo = useState(false); var foldersOpen = fo[0], setFoldersOpen = fo[1];
    var fst = useState({ mode: "all", folders: [] }); var foldersState = fst[0], setFoldersState = fst[1];
    var rk = useState(0); var reloadKey = rk[0], setReloadKey = rk[1];

    function loadFolders() { fetchJSON("/folders").then(setFoldersState).catch(function () {}); }
    useEffect(loadFolders, []);
    function saveFolders(mode, names) {
      fetchJSON("/folders", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: mode, folders: names }) })
        .then(function (res) { setFoldersState(res); setReloadKey(reloadKey + 1); })
        .catch(function (err) { setError(String(err.message || err)); });
    }

    var query = useMemo(function () {
      var qs = new URLSearchParams();
      ["lane", "source", "tag", "date"].forEach(function (k) { if (filters[k]) qs.set(k, filters[k]); });
      var s = qs.toString();
      return s ? "?" + s : "";
    }, [filters.lane, filters.source, filters.tag, filters.date]);

    useEffect(function () {
      setLoading(true); setError(null);
      fetchJSON("/reports" + query).then(function (data) {
        setReports(data.reports || []);
        setFacets(data.facets || { lanes: [], sources: [], tags: [] });
        if (selectedId && !(data.reports || []).some(function (r) { return r.id === selectedId; })) {
          setSelectedId(null); setDetail(null);
        }
      }).catch(function (err) { setError(String(err.message || err)); })
        .finally(function () { setLoading(false); });
    }, [query, reloadKey]);

    useEffect(function () {
      if (!selectedId) return;
      setDetail(null); setError(null);
      fetchJSON("/reports/" + encodeURIComponent(selectedId)).then(setDetail).catch(function (err) {
        setError(String(err.message || err));
      });
    }, [selectedId]);

    function filterInput(name, placeholder) {
      return h("input", {
        className: "hrd-input", value: filters[name], placeholder: placeholder,
        onChange: function (e) {
          var value = e && e.target ? e.target.value : "";
          var next = {}; next[name] = value;
          setFilters(Object.assign({}, filters, next));
        }
      });
    }

    var hasFilters = filters.lane || filters.source || filters.tag || filters.date;

    return h("div", { className: "hrd-root" },
      h("header", { className: "hrd-header" },
        h("div", null,
          h("p", { className: "hrd-eyebrow" }, "Read-only"),
          h("h1", { className: "hrd-title" }, "Report Deck"),
          h("p", { className: "hrd-muted" }, "Browse Markdown reports generated by agents and automation runs. No editing, no actions.")
        ),
        h("div", { className: "hrd-header-actions" },
          h("button", { className: "hrd-btn", onClick: function () { setFoldersOpen(!foldersOpen); loadFolders(); } }, foldersOpen ? "Close folders" : "⚙ Folders"),
          h("button", { className: "hrd-btn", onClick: function () { setFilters({ lane: "", source: "", tag: "", date: "" }); setSelectedId(null); } }, "Clear filters")
        )
      ),
      foldersOpen ? h("section", { className: "hrd-folders-panel" },
        h("div", { className: "hrd-folders-head" },
          h("strong", null, "Folders shown in reports"),
          h("div", { className: "hrd-folders-mode" },
            h("label", null, h("input", { type: "radio", name: "hrd-mode", checked: foldersState.mode === "all", onChange: function () { saveFolders("all", foldersState.visible_folders || []); } }), " All folders"),
            h("label", null, h("input", { type: "radio", name: "hrd-mode", checked: foldersState.mode === "selected", onChange: function () { saveFolders("selected", (foldersState.folders || []).filter(function (f) { return f.visible; }).map(function (f) { return f.name; })); } }), " Selected only")
          )
        ),
        h("div", { className: "hrd-folders-list" },
          (foldersState.folders || []).map(function (f) {
            var selectedMode = foldersState.mode === "selected";
            return h("label", { key: f.name || "(root)", className: "hrd-folder-row" + (selectedMode && !f.visible ? " is-off" : "") },
              h("input", {
                type: "checkbox", disabled: !selectedMode, checked: !!f.visible,
                onChange: function (e) {
                  var cur = (foldersState.folders || []).filter(function (x) { return x.visible; }).map(function (x) { return x.name; });
                  var names = e.target.checked ? cur.concat([f.name]) : cur.filter(function (n) { return n !== f.name; });
                  saveFolders("selected", names);
                }
              }),
              h("span", { className: "hrd-folder-name" }, f.label),
              h("span", { className: "hrd-folder-count" }, f.count)
            );
          }),
          (foldersState.folders || []).length === 0 ? h("p", { className: "hrd-muted" }, "No folders found under the report root.") : null
        ),
        h("p", { className: "hrd-muted hrd-small" }, foldersState.mode === "all" ? "All folders are visible. Switch to “Selected only” to choose." : "Only checked folders appear in the report list.")
      ) : null,
      h("section", { className: "hrd-filters" },
        filterInput("lane", "lane"),
        filterInput("source", "source"),
        filterInput("tag", "tag"),
        filterInput("date", "date (YYYY-MM-DD)"),
        (facets.tags || []).length ? h("div", { className: "hrd-facets" },
          h("span", { className: "hrd-muted" }, "Tags:"),
          (facets.tags || []).map(function (tag) {
            return h("button", { key: tag, className: "hrd-chip" + (filters.tag === tag ? " is-on" : ""), onClick: function () { setFilters(Object.assign({}, filters, { tag: filters.tag === tag ? "" : tag })); } }, tag);
          })
        ) : null
      ),
      error ? h("div", { className: "hrd-error" }, error) : null,
      h("div", { className: "hrd-grid" },
        h("section", { className: "hrd-list" },
          loading ? h("p", { className: "hrd-muted" }, "Loading reports…") : null,
          reports.map(function (report) {
            return h("button", {
              key: report.id,
              className: "hrd-card" + (selectedId === report.id ? " is-selected" : ""),
              onClick: function () { setSelectedId(report.id); }
            },
              h("span", { className: "hrd-time" }, report.generated_at || "undated"),
              h("strong", { className: "hrd-card-title" }, report.title || report.id),
              report.summary ? h("span", { className: "hrd-muted hrd-card-summary" }, report.summary) : null,
              h("span", { className: "hrd-badges" },
                report.lane ? h("span", { className: "hrd-badge" }, report.lane) : null,
                report.source ? h("span", { className: "hrd-badge" }, report.source) : null,
                (report.tags || []).map(function (tag) { return h("span", { key: tag, className: "hrd-badge hrd-badge-tag" }, tag); })
              )
            );
          }),
          !loading && reports.length === 0 ? h("p", { className: "hrd-muted" }, hasFilters ? "No reports match these filters." : "No reports found in the report root.") : null
        ),
        h("article", { className: "hrd-detail" },
          detail ? [
            h("div", { key: "meta", className: "hrd-detail-meta" },
              detail.report.generated_at ? h("span", null, detail.report.generated_at) : null,
              detail.report.source ? h("span", null, detail.report.source) : null,
              detail.report.lane ? h("span", null, detail.report.lane) : null
            ),
            h("div", { key: "md", className: "hrd-markdown" }, markdownBlocks(detail.markdown))
          ] : h("p", { className: "hrd-muted" }, "Select a report to read it.")
        )
      )
    );
  }

  plugins.register("hermes-report-deck", ReportDeck);
})();
