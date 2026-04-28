#!/usr/bin/env python3
"""cc-usage: monitor Claude Code + Codex usage from local JSONL transcripts."""
from __future__ import annotations

__version__ = "0.3.0"

import argparse
import concurrent.futures as cf
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CACHE_VERSION = 5
HOME = Path.home()
PROJECTS_DIR = HOME / ".claude" / "projects"
CODEX_DIR = HOME / ".codex" / "sessions"
CODEX_AUTH = HOME / ".codex" / "auth.json"
CACHE_FILE = HOME / ".cache" / "cc-usage" / "cache.json"

# Pricing per 1M tokens (USD): (input, output, cache_write, cache_read)
# Keyed as "<provider>/<family>". For OpenAI we don't get a separate
# cache-write rate, so cache_write == input.
PRICING = {
    "claude/opus":   (15.00, 75.00, 18.75, 1.50),
    "claude/sonnet": (3.00, 15.00, 3.75, 0.30),
    "claude/haiku":  (1.00, 5.00, 1.25, 0.10),
    # OpenAI public API rates (per 1M)
    "openai/gpt-5":        (1.25, 10.00, 1.25, 0.125),
    "openai/gpt-5-codex":  (1.25, 10.00, 1.25, 0.125),
    "openai/gpt-5.5":      (1.25, 10.00, 1.25, 0.125),
    "openai/gpt-5-mini":   (0.25,  2.00, 0.25, 0.025),
    "openai/gpt-5-nano":   (0.05,  0.40, 0.05, 0.005),
    "openai/o4-mini":      (1.10,  4.40, 1.10, 0.275),
    "openai/o3":           (2.00,  8.00, 2.00, 0.50),
}

# ---- color / tty ----
NO_COLOR = bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()

def c(text, code):
    if NO_COLOR: return str(text)
    return f"\033[{code}m{text}\033[0m"

def dim(t): return c(t, "2")
def bold(t): return c(t, "1")
def green(t): return c(t, "32")
def yellow(t): return c(t, "33")
def red(t): return c(t, "31")
def cyan(t): return c(t, "36")

def cost_color(usd):
    s = fmt_usd(usd)
    if usd >= 5.0: return red(s)
    if usd >= 0.50: return yellow(s)
    return green(s)

def fmt_usd(v):
    if abs(v) < 0.01 and v != 0:
        return f"${v:.4f}"
    return f"${v:.2f}"

def fmt_int(n):
    return f"{n:,}"

# ---- pricing ----
def model_key(model: str, provider: str = "claude") -> str:
    if not model: return "?"
    if model.startswith("<"): return "?"
    m = model.lower()
    if provider == "openai":
        # match longest family substring first
        for fam in sorted(
            [k.split("/",1)[1] for k in PRICING if k.startswith("openai/")],
            key=len, reverse=True
        ):
            if fam in m:
                return f"openai/{fam}"
        return "?"
    for k in PRICING:
        if not k.startswith("claude/"): continue
        fam = k.split("/",1)[1]
        if fam in m:
            return k
    return "?"

def compute_cost(model: str, ti: int, to: int, cw: int, cr: int,
                 provider: str = "claude") -> float:
    k = model_key(model, provider)
    if k not in PRICING:
        return 0.0
    pi, po, pcw, pcr = PRICING[k]
    return (ti*pi + to*po + cw*pcw + cr*pcr) / 1_000_000.0

# ---- context window ----
WINDOW_1M = 1_000_000
WINDOW_DEFAULT = 200_000

def model_window(model: str) -> int:
    if not model:
        return WINDOW_DEFAULT
    m = model.lower()
    if "[1m]" in m or "-1m" in m:
        return WINDOW_1M
    return WINDOW_DEFAULT

def session_window(records) -> int:
    """Infer window. Codex records carry ctx_window directly; else use Claude rules."""
    for r in records:
        if r.get("ctx_window"):
            return int(r["ctx_window"])
    w = WINDOW_DEFAULT
    for r in records:
        if model_window(r.get("model","")) == WINDOW_1M:
            return WINDOW_1M
        ctx = r["ti"] + r["cw"] + r["cr"]
        if ctx > WINDOW_DEFAULT:
            w = WINDOW_1M
    return w

def pct_color(pct):
    s = f"{pct:.1f}%"
    if pct >= 80: return red(s)
    if pct >= 50: return yellow(s)
    return green(s)

def render_bar(pct, width=24):
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct/100.0 * width))
    use_unicode = not NO_COLOR
    full = "█" if use_unicode else "#"
    empty = "░" if use_unicode else "."
    bar = full*filled + empty*(width-filled)
    if NO_COLOR:
        return f"[{bar}]"
    if pct >= 80: color = "31"
    elif pct >= 50: color = "33"
    else: color = "32"
    return f"[{c(bar, color)}]"

# ---- parsing ----
def parse_file(path_str: str):
    """Return list of records: dict per usage entry."""
    records = []
    try:
        with open(path_str, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] != "{":
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                usage = msg.get("usage") or {}
                if not usage:
                    continue
                model = msg.get("model") or ""
                if model.startswith("<"):
                    continue
                ti = int(usage.get("input_tokens") or 0)
                to = int(usage.get("output_tokens") or 0)
                cw = int(usage.get("cache_creation_input_tokens") or 0)
                cr = int(usage.get("cache_read_input_tokens") or 0)
                if ti == 0 and to == 0 and cw == 0 and cr == 0:
                    continue
                mid = msg.get("id") or obj.get("requestId") or ""
                ts = obj.get("timestamp") or ""
                records.append({
                    "id": mid,
                    "ts": ts,
                    "model": model,
                    "ti": ti, "to": to, "cw": cw, "cr": cr,
                    "session": obj.get("sessionId") or "",
                    "cwd": obj.get("cwd") or "",
                    "request": obj.get("requestId") or "",
                    "file": path_str,
                })
    except FileNotFoundError:
        return []
    except Exception:
        return []
    return records

# ---- codex parsing ----
def parse_codex_file(path_str: str):
    """Parse a Codex rollout JSONL. Emits one record per token_count event,
    using last_token_usage (per-call delta) and the most recent turn_context model."""
    records = []
    sid = ""
    cwd = ""
    cur_model = ""
    seq = 0
    try:
        with open(path_str, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] != "{":
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                t = obj.get("type")
                payload = obj.get("payload") or {}
                if t == "session_meta":
                    sid = payload.get("id") or sid
                    cwd = payload.get("cwd") or cwd
                    cur_model = payload.get("model") or cur_model
                    continue
                if t == "turn_context":
                    cur_model = payload.get("model") or cur_model
                    cwd = payload.get("cwd") or cwd
                    continue
                if t != "event_msg":
                    continue
                if payload.get("type") != "token_count":
                    continue
                info = payload.get("info") or {}
                last = info.get("last_token_usage") or {}
                if not last:
                    continue
                ti = int(last.get("input_tokens") or 0)
                cr = int(last.get("cached_input_tokens") or 0)
                # OpenAI's "input_tokens" already includes cached; subtract so
                # ti = non-cached input, matching Claude's accounting style.
                ti = max(0, ti - cr)
                to = int(last.get("output_tokens") or 0)
                ro = int(last.get("reasoning_output_tokens") or 0)
                to_total = to + ro
                if ti == 0 and to_total == 0 and cr == 0:
                    continue
                ts = obj.get("timestamp") or ""
                seq += 1
                records.append({
                    "id": f"{sid}:{ts}:{seq}",  # synthetic stable id
                    "ts": ts,
                    "model": cur_model,
                    "provider": "openai",
                    "ti": ti, "to": to_total, "cw": 0, "cr": cr,
                    "session": sid,
                    "cwd": cwd,
                    "request": "",
                    "file": path_str,
                    "ctx_window": int(info.get("model_context_window") or 0),
                    "rate_limits": payload.get("rate_limits"),
                })
    except FileNotFoundError:
        return []
    except Exception:
        return []
    return records

def codex_auth_mode():
    try:
        with open(CODEX_AUTH, "r") as f:
            return (json.load(f) or {}).get("auth_mode", "?")
    except Exception:
        return "?"

# ---- cache ----
def load_cache():
    try:
        with open(CACHE_FILE, "r") as f:
            data = json.load(f)
        if data.get("version") != CACHE_VERSION:
            return {}
        return data.get("files", {})
    except Exception:
        return {}

def save_cache(files):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump({"version": CACHE_VERSION, "files": files}, f)
    tmp.replace(CACHE_FILE)

# ---- collection ----
def collect_records(window_start_iso=None, debug=False, source="claude"):
    """Collect all dedup'd records. Optionally early-exit on files older than window_start.
    source: "claude" | "codex" | "all"
    """
    if source == "all":
        a, na = collect_records(window_start_iso, debug, "claude")
        b, nb = collect_records(window_start_iso, debug, "codex")
        return a + b, na + nb
    if source == "codex":
        root = CODEX_DIR
        parser = parse_codex_file
        cache_prefix = "codex:"
    else:
        root = PROJECTS_DIR
        parser = parse_file
        cache_prefix = "claude:"
    if not root.exists():
        return [], 0
    paths = list(root.rglob("*.jsonl"))
    n_files = len(paths)
    cache = load_cache()
    new_cache = {}
    to_parse = []

    window_ts = None
    if window_start_iso:
        try:
            window_ts = datetime.fromisoformat(window_start_iso.replace("Z","+00:00")).timestamp()
        except Exception:
            window_ts = None

    cached_records = []
    for p in paths:
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        key = cache_prefix + str(p)
        sig = f"{st.st_mtime}:{st.st_size}"
        cached = cache.get(key)
        # window early-exit: skip files entirely older than window
        if window_ts is not None and st.st_mtime < window_ts:
            # still keep cache entry
            if cached and cached.get("sig") == sig:
                new_cache[key] = cached
            continue
        if cached and cached.get("sig") == sig:
            cached_records.append(cached.get("records", []))
            new_cache[key] = cached
        else:
            to_parse.append((key, sig, str(p)))

    # parse changed/new in parallel
    if to_parse:
        max_workers = min(32, (os.cpu_count() or 4) * 2)
        with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(parser, fp): (k, s) for k, s, fp in to_parse}
            for fut in cf.as_completed(futs):
                k, s = futs[fut]
                try:
                    recs = fut.result()
                except Exception as e:
                    if debug: print(f"parse error {k}: {e}", file=sys.stderr)
                    recs = []
                new_cache[k] = {"sig": s, "records": recs}
                cached_records.append(recs)

    # preserve cache entries for other backends
    for k, v in cache.items():
        if not k.startswith(cache_prefix) and k not in new_cache:
            new_cache[k] = v
    save_cache(new_cache)

    # dedup by id
    seen = set()
    out = []
    for batch in cached_records:
        for r in batch:
            rid = r.get("id") or ""
            if rid and rid in seen:
                continue
            if rid:
                seen.add(rid)
            out.append(r)
    return out, n_files

# ---- aggregation ----
def parse_ts(ts):
    if not ts: return None
    try:
        return datetime.fromisoformat(ts.replace("Z","+00:00"))
    except Exception:
        return None

def day_key(dt):
    if not dt: return "?"
    return dt.astimezone().strftime("%Y-%m-%d")

class Agg:
    __slots__ = ("ti","to","cw","cr","cost","n")
    def __init__(self):
        self.ti=self.to=self.cw=self.cr=0
        self.cost=0.0
        self.n=0
    def add(self, r):
        self.ti += r["ti"]; self.to += r["to"]; self.cw += r["cw"]; self.cr += r["cr"]
        self.cost += compute_cost(r["model"], r["ti"], r["to"], r["cw"], r["cr"],
                                  r.get("provider","claude"))
        self.n += 1
    @property
    def total(self): return self.ti+self.to+self.cw+self.cr

def aggregate_by(records, keyfn):
    out = defaultdict(Agg)
    for r in records:
        k = keyfn(r)
        if k is None: continue
        out[k].add(r)
    return out

# ---- table rendering ----
def render_table(headers, rows, footer=None, aligns=None):
    cols = len(headers)
    if aligns is None:
        aligns = ["l"] + ["r"]*(cols-1)
    all_rows = [headers] + rows + ([footer] if footer else [])
    # compute widths from raw strings (strip ANSI)
    import re
    ansi_re = re.compile(r"\033\[[0-9;]*m")
    def raw(s): return ansi_re.sub("", str(s))
    widths = [max(len(raw(r[i])) for r in all_rows) for i in range(cols)]
    use_unicode = not NO_COLOR
    h = "─" if use_unicode else "-"
    v = "│" if use_unicode else "|"
    tl, tr = ("┌","┐") if use_unicode else ("+","+")
    bl, br = ("└","┘") if use_unicode else ("+","+")
    ml, mr = ("├","┤") if use_unicode else ("+","+")
    cross = "┼" if use_unicode else "+"
    tcross = "┬" if use_unicode else "+"
    bcross = "┴" if use_unicode else "+"

    def hsep(L,M,R):
        return L + M.join(h*(w+2) for w in widths) + R
    def fmt_row(row):
        cells = []
        for i, val in enumerate(row):
            s = str(val)
            pad = widths[i] - len(raw(s))
            if aligns[i] == "r":
                cells.append(" "*pad + s)
            else:
                cells.append(s + " "*pad)
        return v + " " + (" "+v+" ").join(cells) + " " + v

    lines = [hsep(tl,tcross,tr), fmt_row(headers), hsep(ml,cross,mr)]
    for r in rows:
        lines.append(fmt_row(r))
    if footer:
        lines.append(hsep(ml,cross,mr))
        lines.append(fmt_row(footer))
    lines.append(hsep(bl,bcross,br))
    return "\n".join(lines)

def usage_row(label, a: Agg):
    return [label, fmt_int(a.ti), fmt_int(a.to), fmt_int(a.cw), fmt_int(a.cr),
            fmt_int(a.total), cost_color(a.cost)]

USAGE_HEADERS = ["", "input", "output", "cache_w", "cache_r", "total", "cost"]

# ---- windows ----
def now_local():
    return datetime.now().astimezone()

def today_start():
    n = now_local()
    return n.replace(hour=0, minute=0, second=0, microsecond=0)

def days_ago_start(d):
    return today_start() - timedelta(days=d-1)

# ---- commands ----
def cmd_today(args, records):
    start = today_start()
    in_window = [r for r in records if (dt:=parse_ts(r["ts"])) and dt.astimezone() >= start]
    return _by_model_output(args, in_window, title=f"Today ({start.strftime('%Y-%m-%d')})")

def cmd_total(args, records):
    return _by_model_output(args, records, title="All-time totals")

def _by_model_output(args, records, title):
    by_model = aggregate_by(records, lambda r: r["model"] or "?")
    rows = []
    total = Agg()
    for m, a in sorted(by_model.items(), key=lambda kv: -kv[1].cost):
        rows.append(usage_row(dim(m), a))
        total.ti+=a.ti; total.to+=a.to; total.cw+=a.cw; total.cr+=a.cr
        total.cost+=a.cost; total.n+=a.n
    if args.json:
        return _json_out(title, by_model, total)
    out = [bold(title)]
    if not rows:
        out.append(dim("(no usage)"))
    else:
        footer = usage_row(bold("TOTAL"), total)
        out.append(render_table(USAGE_HEADERS, rows, footer))
        out.append(dim(f"{total.n} messages"))
    return "\n".join(out)

def _json_out(title, agg_map, total):
    obj = {"title": title, "groups": {}, "total": _agg_json(total)}
    for k, a in agg_map.items():
        obj["groups"][k] = _agg_json(a)
    return json.dumps(obj, indent=2)

def _agg_json(a: Agg):
    return {"input": a.ti, "output": a.to, "cache_w": a.cw, "cache_r": a.cr,
            "total": a.total, "cost": round(a.cost, 6), "messages": a.n}

def cmd_window_days(args, records, days, title):
    start = days_ago_start(days)
    in_window = [r for r in records if (dt:=parse_ts(r["ts"])) and dt.astimezone() >= start]
    by_day = aggregate_by(in_window, lambda r: day_key(parse_ts(r["ts"])))
    rows = []
    total = Agg()
    for d in sorted(by_day.keys()):
        a = by_day[d]
        rows.append(usage_row(d, a))
        total.ti+=a.ti; total.to+=a.to; total.cw+=a.cw; total.cr+=a.cr
        total.cost+=a.cost; total.n+=a.n
    if args.json:
        return _json_out(title, by_day, total)
    out = [bold(title)]
    if not rows:
        out.append(dim("(no usage)"))
    else:
        footer = usage_row(bold("TOTAL"), total)
        out.append(render_table(USAGE_HEADERS, rows, footer))
        out.append(dim(f"{total.n} messages"))
    return "\n".join(out)

def cmd_week(args, records):
    return cmd_window_days(args, records, 7, "Last 7 days")

def cmd_month(args, records):
    return cmd_window_days(args, records, 30, "Last 30 days")

def cmd_session(args, records):
    sid = args.id
    if not sid:
        # current session = most recent message
        latest = max(records, key=lambda r: r["ts"] or "", default=None)
        if not latest:
            return "(no sessions)"
        sid = latest["session"]
    sess = [r for r in records if r["session"] == sid]
    if not sess:
        return f"No records for session {sid}"
    sess.sort(key=lambda r: r["ts"] or "")
    by_model = aggregate_by(sess, lambda r: r["model"] or "?")
    total = Agg()
    rows = []
    for m, a in sorted(by_model.items(), key=lambda kv: -kv[1].cost):
        rows.append(usage_row(dim(m), a))
        total.ti+=a.ti; total.to+=a.to; total.cw+=a.cw; total.cr+=a.cr
        total.cost+=a.cost; total.n+=a.n
    t0 = parse_ts(sess[0]["ts"])
    t1 = parse_ts(sess[-1]["ts"])
    dur = (t1-t0) if t0 and t1 else timedelta(0)
    # context window: latest assistant message in session
    latest = sess[-1]
    win = session_window(sess)
    ctx_used = latest["ti"] + latest["cw"] + latest["cr"]
    ctx_pct = ctx_used / win * 100.0 if win else 0.0
    if args.json:
        obj = _json_obj_for_session(sid, sess, by_model, total, dur)
        obj["context"] = {"used": ctx_used, "window": win, "pct": round(ctx_pct, 2),
                          "model": latest["model"]}
        return json.dumps(obj, indent=2)
    out = [bold(f"Session {sid}")]
    out.append(dim(f"cwd: {sess[0]['cwd']}"))
    out.append(dim(f"messages: {total.n}   duration: {_fmt_dur(dur)}   start: {sess[0]['ts']}"))
    out.append(f"Context: {fmt_int(ctx_used)} / {fmt_int(win)} ({pct_color(ctx_pct)})  "
               f"{dim(latest['model'])}")
    out.append(render_table(USAGE_HEADERS, rows, usage_row(bold("TOTAL"), total)))
    return "\n".join(out)

def _fmt_dur(td: timedelta):
    s = int(td.total_seconds())
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h: return f"{h}h{m}m"
    if m: return f"{m}m{s}s"
    return f"{s}s"

def _json_obj_for_session(sid, sess, by_model, total, dur):
    return {
        "session": sid, "cwd": sess[0]["cwd"], "messages": total.n,
        "duration_seconds": int(dur.total_seconds()),
        "start": sess[0]["ts"], "end": sess[-1]["ts"],
        "models": {k:_agg_json(v) for k,v in by_model.items()},
        "total": _agg_json(total),
    }

def cmd_sessions(args, records):
    by_sess = defaultdict(lambda: {"agg": Agg(), "cwd": "", "ts0": "", "ts1": "",
                                    "recs": []})
    for r in records:
        s = r["session"]
        if not s: continue
        b = by_sess[s]
        b["agg"].add(r)
        b["cwd"] = r["cwd"] or b["cwd"]
        b["recs"].append(r)
        if not b["ts0"] or r["ts"] < b["ts0"]: b["ts0"] = r["ts"]
        if not b["ts1"] or r["ts"] > b["ts1"]: b["ts1"] = r["ts"]
    # compute context % per session
    for s, b in by_sess.items():
        recs = sorted(b["recs"], key=lambda r: r["ts"] or "")
        latest = recs[-1]
        win = session_window(recs)
        used = latest["ti"] + latest["cw"] + latest["cr"]
        b["ctx_used"] = used
        b["ctx_win"] = win
        b["ctx_pct"] = used / win * 100.0 if win else 0.0
    items = sorted(by_sess.items(), key=lambda kv: -kv[1]["agg"].cost)[:20]
    if args.json:
        return json.dumps([{"session": s, "cwd": b["cwd"], "start": b["ts0"], "end": b["ts1"],
                            "context_used": b["ctx_used"], "context_window": b["ctx_win"],
                            "context_pct": round(b["ctx_pct"], 2),
                            **_agg_json(b["agg"])} for s,b in items], indent=2)
    headers = ["session", "cwd", "msgs", "tokens", "cost", "ctx%", "start"]
    rows = []
    for s, b in items:
        a = b["agg"]
        cwd = b["cwd"] or ""
        if len(cwd) > 40: cwd = "..." + cwd[-37:]
        rows.append([dim(s[:8]), cwd, fmt_int(a.n), fmt_int(a.total), cost_color(a.cost),
                     pct_color(b["ctx_pct"]),
                     (b["ts0"] or "")[:16].replace("T", " ")])
    return bold("Top 20 sessions by cost") + "\n" + render_table(headers, rows,
        aligns=["l","l","r","r","r","r","l"])

def cmd_projects(args, records):
    by_p = aggregate_by(records, lambda r: r["cwd"] or "?")
    items = sorted(by_p.items(), key=lambda kv: -kv[1].cost)
    total = Agg()
    rows = []
    for p, a in items:
        disp = p
        if len(disp) > 50: disp = "..." + disp[-47:]
        rows.append([disp, fmt_int(a.n), fmt_int(a.total), cost_color(a.cost)])
        total.ti+=a.ti; total.to+=a.to; total.cw+=a.cw; total.cr+=a.cr
        total.cost+=a.cost; total.n+=a.n
    if args.json:
        return _json_out("projects", by_p, total)
    headers = ["project (cwd)", "msgs", "tokens", "cost"]
    footer = [bold("TOTAL"), fmt_int(total.n), fmt_int(total.total), cost_color(total.cost)]
    return bold("Cost by project") + "\n" + render_table(headers, rows, footer,
        aligns=["l","r","r","r"])

def _latest_assistant_in_file(path):
    """Read a JSONL file and return the latest assistant usage record (by timestamp).
    Reads whole file (most are small)."""
    latest = None
    latest_ts = ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] != "{":
                    continue
                # cheap pre-filter
                if '"assistant"' not in line or '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                usage = msg.get("usage") or {}
                if not usage:
                    continue
                ts = obj.get("timestamp") or ""
                if ts < latest_ts:
                    continue
                model = msg.get("model") or ""
                if model.startswith("<"):
                    continue
                ti = int(usage.get("input_tokens") or 0)
                to = int(usage.get("output_tokens") or 0)
                cw = int(usage.get("cache_creation_input_tokens") or 0)
                cr = int(usage.get("cache_read_input_tokens") or 0)
                if ti==0 and to==0 and cw==0 and cr==0:
                    continue
                latest_ts = ts
                latest = {
                    "ts": ts, "model": model, "ti": ti, "to": to, "cw": cw, "cr": cr,
                    "session": obj.get("sessionId") or "", "cwd": obj.get("cwd") or "",
                    "file": str(path),
                }
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return latest

def _latest_codex_in_file(path):
    recs = parse_codex_file(str(path))
    if not recs: return None
    recs.sort(key=lambda r: r["ts"] or "")
    return recs[-1]

def find_current_session(source="claude"):
    """Find most-recently-modified JSONL and return its latest assistant record.
    Falls back across the top few most-recent files if newest has no assistant usage."""
    if source == "codex":
        root = CODEX_DIR
        latest_fn = _latest_codex_in_file
    else:
        root = PROJECTS_DIR
        latest_fn = _latest_assistant_in_file
    if not root.exists():
        return None
    paths = []
    for p in root.rglob("*.jsonl"):
        try:
            paths.append((p.stat().st_mtime, p))
        except FileNotFoundError:
            continue
    if not paths:
        return None
    paths.sort(reverse=True)
    # try top 5 most recently modified files
    best = None
    for _, p in paths[:5]:
        rec = latest_fn(p)
        if rec is None:
            continue
        if best is None or rec["ts"] > best["ts"]:
            best = rec
    return best

def cmd_ctx(args, _records=None):
    src = getattr(args, "source", "claude")
    if src == "all":
        # show both
        outs = []
        for s in ("claude", "codex"):
            args2 = argparse.Namespace(**{**vars(args), "source": s})
            outs.append(bold(f"[{s}]"))
            outs.append(cmd_ctx(args2))
        return "\n".join(outs)
    rec = find_current_session(source=src)
    if not rec:
        if args.json:
            return json.dumps({"error": "no current session", "source": src})
        return f"(no current {src} session found)"
    # determine window
    if rec.get("ctx_window"):
        win = int(rec["ctx_window"])
    else:
        win = model_window(rec["model"])
    if src == "claude" and win == WINDOW_DEFAULT:
        # heuristic: scan same file for any usage > 200k
        try:
            with open(rec["file"], "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"input_tokens"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    msg = obj.get("message") or {}
                    u = msg.get("usage") or {}
                    if not u: continue
                    ctx = int(u.get("input_tokens") or 0) + \
                          int(u.get("cache_creation_input_tokens") or 0) + \
                          int(u.get("cache_read_input_tokens") or 0)
                    if ctx > WINDOW_DEFAULT:
                        win = WINDOW_1M
                        break
        except Exception:
            pass
    used = rec["ti"] + rec["cw"] + rec["cr"]
    pct = used / win * 100.0 if win else 0.0
    if args.json:
        return json.dumps({
            "session": rec["session"], "model": rec["model"],
            "context_used": used, "context_window": win,
            "context_pct": round(pct, 2),
            "input_tokens": rec["ti"], "cache_creation_input_tokens": rec["cw"],
            "cache_read_input_tokens": rec["cr"], "output_tokens": rec["to"],
            "timestamp": rec["ts"], "cwd": rec["cwd"],
        }, indent=2)
    sid = rec["session"][:8] if rec["session"] else "?"
    bar = render_bar(pct)
    line1 = f"Session {bold(sid)}  {dim(rec['model'])}"
    line2 = f"{bar} {fmt_int(used)} / {fmt_int(win)}  ({pct_color(pct)})"
    return line1 + "\n" + line2

def _fmt_window_label(minutes):
    if not minutes: return "?"
    m = int(minutes)
    if m % 1440 == 0 and m >= 1440:
        d = m // 1440
        return "weekly" if d == 7 else f"{d}d"
    if m % 60 == 0:
        return f"{m // 60}h"
    return f"{m}m"

def _fmt_reset_in(seconds):
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d: return f"{d}d {h}h"
    if h: return f"{h}h {m}m"
    return f"{m}m"

def _latest_rate_limits(records):
    """Return (rate_limits_dict, source_record) for the latest non-null block, or (None, None)."""
    best = None
    for r in records:
        rl = r.get("rate_limits")
        if not rl:
            continue
        if best is None or (r.get("ts") or "") > (best.get("ts") or ""):
            best = r
    return (best.get("rate_limits"), best) if best else (None, None)

def cmd_limits(args, records):
    src = getattr(args, "source", "claude")
    if src == "claude":
        msg = "limits is Codex-only; rerun with --codex (Claude Code does not expose subscription windows locally)."
        if args.json:
            return json.dumps({"error": "claude source not supported", "hint": "use --codex"})
        return msg
    rl, rec = _latest_rate_limits(records)
    if not rl:
        if args.json:
            return json.dumps({"error": "no rate_limits data", "hint": "run any codex command under chatgpt oauth"})
        return ("(no rate_limits data found in any codex session)\n"
                + dim("hint: run a codex command under chatgpt oauth, then retry"))
    primary = rl.get("primary") or {}
    secondary = rl.get("secondary") or {}
    plan_type = rl.get("plan_type") or "?"
    auth_mode = codex_auth_mode()
    rec_ts = rec.get("ts") or ""

    def _window_row(label_fallback, w):
        if not w:
            return None
        used = float(w.get("used_percent") or 0.0)
        wm = w.get("window_minutes")
        label = _fmt_window_label(wm) if wm else label_fallback
        resets_at = int(w.get("resets_at") or 0)
        if resets_at:
            now_ts = time.time()
            delta = _fmt_reset_in(resets_at - now_ts)
            local = datetime.fromtimestamp(resets_at, tz=timezone.utc).astimezone()
            resets_str = f"{delta}  ({local.strftime('%a %H:%M')})"
        else:
            resets_str = "?"
        return [label, pct_color(used), render_bar(used, width=22), resets_str], used, wm, resets_at

    rows_data = []
    for fb, w in (("primary", primary), ("secondary", secondary)):
        r = _window_row(fb, w)
        if r:
            rows_data.append((fb, r))

    if args.json:
        out = {
            "auth_mode": auth_mode,
            "plan_type": plan_type,
            "captured_at": rec_ts,
            "source_file": rec.get("file"),
            "limit_id": rl.get("limit_id"),
            "rate_limit_reached_type": rl.get("rate_limit_reached_type"),
            "windows": {},
        }
        now_ts = time.time()
        for key, (_row, used, wm, resets_at) in rows_data:
            out["windows"][key] = {
                "used_percent": used,
                "window_minutes": wm,
                "resets_at": resets_at,
                "resets_in_seconds": int(max(0, (resets_at - now_ts))) if resets_at else None,
            }
        return json.dumps(out, indent=2)

    headers = ["window", "used", "bar", "resets in"]
    rows = [r[1][0] for r in rows_data]
    out = [bold(f"Codex subscription (plan: {plan_type})") + dim(f"  auth_mode={auth_mode}")]
    if rows:
        out.append(render_table(headers, rows, aligns=["l", "r", "l", "l"]))
    else:
        out.append(dim("(no windows reported)"))
    reached = rl.get("rate_limit_reached_type")
    if reached:
        out.append(red(f"!! rate limit reached: {reached}"))
    out.append(dim(f"captured: {rec_ts}"))
    out.append(dim(f"source: {rec.get('file')}"))
    return "\n".join(out)

def cmd_live(args, records):
    """Watch loop: refresh every 2s."""
    try:
        while True:
            records, _ = collect_records(window_start_iso=today_start().astimezone(timezone.utc).isoformat(),
                                         debug=args.debug, source=getattr(args,"source","claude"))
            start = today_start()
            todays = [r for r in records if (dt:=parse_ts(r["ts"])) and dt.astimezone() >= start]
            todays.sort(key=lambda r: r["ts"] or "")
            total = Agg()
            for r in todays: total.add(r)
            sys.stdout.write("\033[2J\033[H" if not NO_COLOR else "\n"*3)
            print(bold(f"cc-usage live  ({datetime.now().strftime('%H:%M:%S')})"))
            # current session context
            cur = find_current_session(source=getattr(args,"source","claude"))
            if cur:
                win = model_window(cur["model"])
                used = cur["ti"] + cur["cw"] + cur["cr"]
                # quick 1M heuristic from same-file scan via session records
                sid = cur["session"]
                if sid:
                    sess_recs = [r for r in records if r["session"] == sid]
                    if sess_recs:
                        win = session_window(sess_recs)
                pct = used/win*100.0 if win else 0.0
                bar = render_bar(pct, width=32)
                sid_short = sid[:8] if sid else "?"
                print(bold(f"  CONTEXT  ") + bar +
                      f"  {fmt_int(used)} / {fmt_int(win)}  ({pct_color(pct)})  "
                      f"{dim(sid_short)} {dim(cur['model'])}")
            print(dim(f"today: {start.strftime('%Y-%m-%d')}"))
            print(f"  total cost: {cost_color(total.cost)}   messages: {total.n}   "
                  f"tokens: {fmt_int(total.total)}")
            print()
            print(bold("last 5 messages:"))
            recent = todays[-5:]
            if not recent:
                print(dim("  (none yet today)"))
            else:
                rows = []
                for r in recent:
                    cost = compute_cost(r["model"], r["ti"], r["to"], r["cw"], r["cr"])
                    ts = (r["ts"] or "")[11:19]
                    cwd = r["cwd"] or ""
                    if len(cwd) > 30: cwd = "..." + cwd[-27:]
                    rows.append([ts, dim(model_key(r["model"])), cwd,
                                 fmt_int(r["ti"]+r["to"]+r["cw"]+r["cr"]), cost_color(cost)])
                print(render_table(["time","model","cwd","tokens","cost"], rows,
                                   aligns=["l","l","l","r","r"]))
            print()
            print(dim("(ctrl-c to exit; refresh every 2s)"))
            sys.stdout.flush()
            time.sleep(2)
    except KeyboardInterrupt:
        return ""

# ---- main ----
def build_parser():
    p = argparse.ArgumentParser(prog="cc-usage",
        description="Monitor Claude Code + Codex usage from local JSONL transcripts.")
    p.add_argument("--version", action="version", version=f"cc-usage {__version__}")
    p.add_argument("--json", action="store_true", help="output JSON instead of pretty table")
    p.add_argument("--debug", action="store_true", help="print parse errors to stderr")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--codex", dest="source", action="store_const", const="codex",
                     help="read from ~/.codex/sessions instead of Claude Code")
    src.add_argument("--all", dest="source", action="store_const", const="all",
                     help="combine Claude Code and Codex")
    p.set_defaults(source="claude")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("today")
    sub.add_parser("week")
    sub.add_parser("month")
    sub.add_parser("total")
    sub.add_parser("sessions")
    sub.add_parser("projects")
    sub.add_parser("live")
    sub.add_parser("ctx")
    sub.add_parser("limits")
    s = sub.add_parser("session")
    s.add_argument("id", nargs="?", default=None)
    return p

def main():
    parser = build_parser()
    args = parser.parse_args()
    cmd = args.cmd or "today"

    t0 = time.time()
    window_iso = None
    if cmd == "today":
        window_iso = today_start().astimezone(timezone.utc).isoformat()
    elif cmd == "week":
        window_iso = days_ago_start(7).astimezone(timezone.utc).isoformat()
    elif cmd == "month":
        window_iso = days_ago_start(30).astimezone(timezone.utc).isoformat()

    if cmd == "live":
        cmd_live(args, [])
        return

    if cmd == "ctx":
        # Fast path: don't scan everything
        out = cmd_ctx(args)
        if out:
            print(out)
        return

    records, n_files = collect_records(window_start_iso=window_iso, debug=args.debug,
                                       source=args.source)
    elapsed = time.time() - t0

    # subscription notice for codex
    if args.source in ("codex", "all") and not args.json:
        mode = codex_auth_mode()
        if mode == "chatgpt":
            print(dim("note: Codex auth_mode=chatgpt — costs shown are API-equivalent "
                      "estimates, not what you actually pay."))
        elif mode == "apikey":
            print(dim("note: Codex auth_mode=apikey — costs reflect OpenAI public pricing."))

    handlers = {
        "today": cmd_today, "week": cmd_week, "month": cmd_month,
        "total": cmd_total, "session": cmd_session, "sessions": cmd_sessions,
        "projects": cmd_projects, "limits": cmd_limits,
    }
    out = handlers[cmd](args, records)
    if out:
        print(out)
    if not args.json and args.debug:
        print(dim(f"\n[scanned {n_files} files, {len(records)} dedup'd records, {elapsed:.2f}s]"),
              file=sys.stderr)

if __name__ == "__main__":
    main()
