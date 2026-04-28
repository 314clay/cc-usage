# Codex `rate_limits` schema (v0.3 discovery)

Captured from a fresh oauth session on 2026-04-28 after switching `auth_mode` from `apikey` to `chatgpt`.

Source file: `~/.codex/sessions/2026/04/28/rollout-2026-04-28T13-02-56-019dd579-188e-7503-a952-d94ff8d8ef1a.jsonl`

## Where the field lives

Correction to earlier assumption: `rate_limits` is a **sibling of `info`**, not nested inside it.

```
{
  "timestamp": "...",
  "type": "event_msg",
  "payload": {
    "type": "token_count",
    "info": { ... } | null,
    "rate_limits": { ... } | null
  }
}
```

The parser hook for v0.3 is therefore `payload.get("rate_limits")` directly, **not** `payload.info.rate_limits`. This means in `cc_usage.py:201–252` the read happens at the same indent level as `info = payload.get("info")` (cc_usage.py:225).

## Frequency

In a 12-line session with one prompt + one response there are **2 records** carrying `rate_limits`, both on `event_msg` / `token_count`:

1. First record (pre-response): `info: null`, `rate_limits: {...}` populated.
2. Second record (post-response): `info: {...}` populated, `rate_limits: {...}` updated.

For a "current usage" view we want the **latest non-null `rate_limits` across all session files**, ranked by record timestamp.

## Schema

```json
{
  "limit_id": "codex",
  "limit_name": null,
  "primary":   { "used_percent": 0.0, "window_minutes": 300,   "resets_at": 1777420981 },
  "secondary": { "used_percent": 0.0, "window_minutes": 10080, "resets_at": 1778007781 },
  "credits": null,
  "plan_type": "prolite",
  "rate_limit_reached_type": null
}
```

| Field | Type | Notes |
|---|---|---|
| `limit_id` | string | Always `"codex"` for this product. |
| `limit_name` | string \| null | Null in sample. Likely set when `rate_limit_reached_type` fires. |
| `primary.used_percent` | float | 0–100. Percent of cap consumed in the short window. |
| `primary.window_minutes` | int | `300` = 5-hour rolling window. |
| `primary.resets_at` | int (unix ts) | When the short window resets. `1777420981` = 2026-04-29 00:03:01 UTC. |
| `secondary.used_percent` | float | 0–100. Percent of cap consumed in the long window. |
| `secondary.window_minutes` | int | `10080` = 7-day weekly window. |
| `secondary.resets_at` | int (unix ts) | When the weekly window resets. |
| `credits` | object \| null | Null on chatgpt plan; presumably populated for credit-based plans. |
| `plan_type` | string | Observed `"prolite"`. Other values likely `"plus"`, `"pro"`, `"business"`, etc. |
| `rate_limit_reached_type` | string \| null | Set to which window tripped when limited. |

## Units & semantics

- **`used_percent`** is already a percentage (0–100), not raw count, not 0–1. Render directly with the existing `pct_color()` (cc_usage.py:124–128) and `render_bar()` (cc_usage.py:130–142).
- **`resets_at`** is unix epoch seconds. Convert with `datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()` for local-time display, plus a "in Xh Ym" delta against `now()`.
- **Window naming** for display: `primary` → "5h" or "Hourly", `secondary` → "Weekly". Don't hardcode 300/10080; pull from `window_minutes` and format (`f"{m//60}h"` for ≤24h, `f"{m//1440}d"` otherwise).

## Display sketch — `cc-usage --codex limits`

```
Codex subscription (plan: prolite) — auth_mode=chatgpt

+---------+--------+------------------------+-------------------+
| window  |  used  | bar                    | resets in         |
+---------+--------+------------------------+-------------------+
| 5h      |  0.0%  | ░░░░░░░░░░░░░░░░░░░░░░ | 5h 0m  (00:03)    |
| weekly  |  0.0%  | ░░░░░░░░░░░░░░░░░░░░░░ | 6d 23h (Mon 12:03)|
+---------+--------+------------------------+-------------------+
source: <path to latest session file with non-null rate_limits>
```

`--json` shape (machine-readable):

```json
{
  "auth_mode": "chatgpt",
  "plan_type": "prolite",
  "captured_at": "2026-04-28T19:03:03Z",
  "source_file": "/.../rollout-...jsonl",
  "windows": {
    "primary":   { "used_percent": 0.0, "window_minutes": 300,   "resets_at": 1777420981, "resets_in_seconds": 17988 },
    "secondary": { "used_percent": 0.0, "window_minutes": 10080, "resets_at": 1778007781, "resets_in_seconds": 604788 }
  },
  "rate_limit_reached_type": null
}
```

## Implications for v0.3 implementation

1. **Parser change** (`parse_codex_file()` cc_usage.py:201–252): also extract `payload.get("rate_limits")` into the per-record dict. Keep raw — don't flatten.
2. **Cache bump**: `CACHE_VERSION = 4 → 5` (cc_usage.py:17) since record schema changes.
3. **New subcommand `limits`**: `sub.add_parser("limits")` after cc_usage.py:865. For `args.source == "codex"` (or default when `--codex` is used), find the most recent record across all parsed files with a non-null `rate_limits` and render the table above. For `args.source == "claude"`, print "no subscription rate-limit data available for Claude Code" and exit 0.
4. **Reuse existing helpers**: `pct_color()`, `render_bar()`, `render_table()`, `dim()` for the source-file footer, `_json_out()` (or equivalent) for `--json`.
5. **No API call needed** — everything reads from local JSONL.

## Verification

- `~/.codex/sessions/2026/04/28/rollout-...19dd579...jsonl` exists and is newer than `~/.codex/auth.json` ✓
- Two `rate_limits` blocks present, both with populated structure ✓
- Schema documented with field types and units ✓

Next step: separate planning round for the actual code changes.
