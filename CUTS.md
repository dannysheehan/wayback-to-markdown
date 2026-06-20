# Ponytail Audit — Planned Cuts

Ranked biggest cut first. All changes are in `wayback_to_markdown.py`.

---

## 1. `yagni` — Dead fallback parser (~50 lines removed)

`BasicHTMLText` (lines 345–373) is a stdlib-only fallback activated when
`beautifulsoup4` is absent, but `beautifulsoup4` is a **hard dependency** in
`pyproject.toml`. The fallback can never be reached in a correctly installed
environment.

**Cut:**
- `try/except ImportError` shim around `bs4` imports (lines 20–26)
- `BasicHTMLText` class (lines 345–373)
- All `BeautifulSoup is None` / `BeautifulSoup is not None` guards in
  `inline_md`, `block_md`, and `html_to_markdown`

**Replacement:** Import `bs4` directly; remove all conditional guards.

---

## 2. `shrink` — Duplicated alternate-capture fallback block (~15 lines removed)

In `fetch_archived_page`, the `HTTPError` branch and the bare `except
Exception` branch contain identical code:

```python
loaded_alternates = True
alternates = load_alternate_captures(...)
seen = {record["timestamp"]}
candidates.extend(...)
seen.update(...)
continue
```

**Cut:** Extract to an inline helper `_load_and_queue_alternates` and call it
from both branches.

---

## 3. `shrink` — Shared path-derivation logic (~12 lines removed)

`output_relative_path` and `raw_html_path` both:
1. Parse the URL
2. Unquote and strip the path
3. Split and slugify each segment
4. Append a SHA-1 collision suffix when the path is already used

**Cut:** Extract a `_derive_base_path(original, used)` helper that does steps
1–3, then each function only handles its own suffix and collision logic.

---

## 4. `shrink` — Repeated `isinstance` guards in `inline_md` / `block_md` (~8 lines removed)

Both `inline_md` and `block_md` open with the same three guards:

```python
if BeautifulSoup is not None and isinstance(node, Comment): return ""
if BeautifulSoup is not None and isinstance(node, NavigableString): return ...
if BeautifulSoup is None or not isinstance(node, Tag): return ...
```

After removing the `BeautifulSoup is None` wrappers (cut #1), these collapse
to simple `isinstance` checks with no duplication.  The guards remain correct
but are now 3 plain lines each instead of conditional chains.

---

## 5. `stdlib` — `yaml_string` one-liner wrapper (~3 lines removed)

```python
def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
```

This is a named alias for `json.dumps`. Delete it; call `json.dumps(...,
ensure_ascii=False)` at all 5 call sites in `write_markdown`.

---

## 6. `shrink` — `cdx_url` hand-rolled multi-value encoding (~5 lines removed)

The dict-iteration + `isinstance(value, list)` flattening loop (lines 112–120)
replicates what `cdx_captures_url` already does by building the list literal
directly.

**Cut:** Replace the `params` dict + loop with a plain `pairs` list literal,
matching the style already used in `cdx_captures_url`.

---

## Net

| Cut | Lines removed |
|-----|--------------|
| Dead fallback parser + guards | ~50 |
| Duplicate alternate-capture block | ~15 |
| Shared path-derivation helper | ~12 |
| `isinstance` guard simplification | ~8 |
| `yaml_string` wrapper | ~3 |
| `cdx_url` encoding loop | ~5 |
| **Total** | **~93** |

Zero dependencies added or removed.
