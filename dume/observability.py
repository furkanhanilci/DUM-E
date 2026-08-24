"""WP-048 — the operator's view.

A read-only window onto what the harness is doing, served from the standard
library so it runs wherever the harness runs and adds nothing to install.

Read-only on purpose. Every action already has a home in the command gateway,
where it is authenticated, classified, rate-limited and audited; a button here
would be a second, weaker way to do the same thing. What a status page is good
at is showing a person what is true, and that is all this does.
"""
from __future__ import annotations

import html
import json
import socket
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Colour carries meaning and never carries it alone: every state is also spelled
# out, so nothing is visible only to someone who can see the hue.
STATE_COLOUR = {
    "ACCEPTED": "#009E73", "TECH_COMPLETE": "#44AA99",
    "ACCEPTANCE_READY": "#0072B2", "VERIFYING": "#56B4E9",
    "CODE_REVIEW": "#56B4E9", "SPEC_REVIEW": "#56B4E9",
    "EXECUTING": "#E69F00", "PLANNED": "#E69F00", "PACKAGED": "#E69F00",
    "READY": "#F0E442", "DISCOVERED": "#63666A",
    "BLOCKED": "#D55E00", "FAILED": "#D55E00", "RETRY": "#CC79A7",
}


def _reachable(url: str, timeout: float = 1.5) -> bool:
    try:
        urllib.request.urlopen(url, timeout=timeout).read(1)
        return True
    except (urllib.error.URLError, OSError):
        return False


def _containers() -> list[dict]:
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in out.strip().splitlines():
        name, _, status = line.partition("\t")
        if name.startswith(("dume-", "buzz-prod-")):
            rows.append({"name": name, "status": status,
                         "healthy": "healthy" in status})
    return rows


def snapshot() -> dict:
    """Everything the page shows, gathered once."""
    from .research import survey
    from .runtimes.profiles import RuntimeRegistry
    from .state import Store

    data: dict = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    db = REPO / "state" / "dume.db"
    packages, counts = [], {}
    if db.is_file():
        store = Store(db)
        try:
            for row in store.all_wps():
                counts[row["state"]] = counts.get(row["state"], 0) + 1
                if row["state"] != "DISCOVERED":
                    packages.append({
                        "wp_id": row["wp_id"], "title": row["title"],
                        "state": row["state"], "wave": row["wave"],
                        "candidate": (row["candidate_revision"] or "")[:12],
                        "waiting_on": store.unmet_dependencies(row["wp_id"]),
                    })
        finally:
            store.close()
    data["packages"] = packages
    data["counts"] = counts

    try:
        registry = RuntimeRegistry.load()
        data["runtimes"] = registry.table()
    except Exception:
        data["runtimes"] = []

    data["services"] = _containers()
    data["endpoints"] = {
        "buzz relay": _reachable("http://127.0.0.1:3000/"),
        "qwen (GPU 0)": _reachable("http://127.0.0.1:8000/v1/models"),
        "mistral (GPU 1)": _reachable("http://127.0.0.1:8001/v1/models"),
    }

    try:
        data["research"] = survey()
    except Exception:
        data["research"] = {}

    live = REPO / "evidence" / "live" / "run_result.json"
    data["last_run"] = None
    if live.is_file():
        try:
            report = json.loads(live.read_text())
            data["last_run"] = {
                "wp_id": report.get("wp_id"),
                "verdict": report.get("verdict"),
                "seconds": report.get("elapsed_seconds"),
                "channel": report.get("channel"),
                "steps": report.get("steps", []),
                "bindings": report.get("bindings", {}),
            }
        except json.JSONDecodeError:
            pass

    paused = REPO / "state" / "PAUSED"
    data["paused"] = paused.read_text().strip() if paused.is_file() else None
    return data


def _chip(text: str, colour: str) -> str:
    return (f'<span class="chip" style="--c:{colour}">{html.escape(text)}</span>')


def render(data: dict) -> str:
    e = html.escape
    parts: list[str] = []

    if data.get("paused"):
        parts.append(f'<div class="banner">PAUSED — {e(data["paused"])}. '
                     'No new work will start; work already running finishes.</div>')

    parts.append('<section><h2>Services</h2><div class="grid">')
    for name, up in data["endpoints"].items():
        parts.append(f'<div class="card"><div class="k">{e(name)}</div>'
                     f'<div class="v">{_chip("reachable" if up else "unreachable", "#009E73" if up else "#D55E00")}</div></div>')
    for service in data["services"]:
        parts.append(f'<div class="card"><div class="k">{e(service["name"])}</div>'
                     f'<div class="v">{_chip(service["status"], "#009E73" if service["healthy"] else "#E69F00")}</div></div>')
    parts.append("</div></section>")

    parts.append('<section><h2>Runtimes</h2><table><tr><th>runtime</th><th>status</th>'
                 '<th>mode</th><th>family</th><th>qualified for</th></tr>')
    for row in data["runtimes"]:
        colour = "#009E73" if row["status"] == "AVAILABLE" else (
            "#63666A" if row["status"] == "UNKNOWN" else "#D55E00")
        roles = ", ".join(row["qualified_for"]) or "— not qualified, so not eligible"
        parts.append(f'<tr><td><b>{e(row["runtime"])}</b></td>'
                     f'<td>{_chip(row["status"], colour)}</td><td>{e(row["mode"])}</td>'
                     f'<td>{e(row["family"] or "—")}</td><td class="muted">{e(roles)}</td></tr>')
    parts.append("</table></section>")

    parts.append('<section><h2>Commissioning</h2><div class="grid">')
    for state, count in sorted(data["counts"].items(), key=lambda kv: -kv[1]):
        parts.append(f'<div class="card"><div class="k">{e(state)}</div>'
                     f'<div class="v big" style="color:{STATE_COLOUR.get(state, "#63666A")}">{count}</div></div>')
    parts.append("</div>")
    if data["packages"]:
        parts.append('<table><tr><th>package</th><th>state</th><th>candidate</th>'
                     '<th>waiting on</th></tr>')
        for pkg in data["packages"]:
            waiting = ", ".join(pkg["waiting_on"]) or "—"
            parts.append(
                f'<tr><td><b>{e(pkg["wp_id"])}</b> <span class="muted">{e(pkg["title"][:48])}</span></td>'
                f'<td>{_chip(pkg["state"], STATE_COLOUR.get(pkg["state"], "#63666A"))}</td>'
                f'<td class="mono">{e(pkg["candidate"] or "—")}</td>'
                f'<td class="muted">{e(waiting)}</td></tr>')
        parts.append("</table>")
    parts.append("</section>")

    run = data.get("last_run")
    if run:
        verdict_colour = {"MERGE_ELIGIBLE": "#009E73", "FAILED": "#D55E00",
                          "BLOCKED_RUNTIME": "#E69F00"}.get(run["verdict"], "#63666A")
        parts.append(f'<section><h2>Last live run — {e(run["wp_id"] or "")} '
                     f'{_chip(run["verdict"] or "?", verdict_colour)}</h2>')
        if run.get("bindings"):
            parts.append('<p class="muted">')
            parts.append(" · ".join(
                f'{e(role)} → {e(b["runtime_id"])}'
                for role, b in run["bindings"].items()))
            parts.append("</p>")
        parts.append('<table><tr><th>step</th><th></th><th>detail</th></tr>')
        for step in run["steps"]:
            colour = {"OK": "#009E73", "FAILED": "#D55E00",
                      "BLOCKED": "#E69F00"}.get(step["outcome"], "#63666A")
            parts.append(f'<tr><td class="mono">{e(step["name"])}</td>'
                         f'<td>{_chip(step["outcome"], colour)}</td>'
                         f'<td class="muted">{e(step["detail"][:160])}</td></tr>')
        parts.append("</table></section>")

    research = data.get("research") or {}
    if research:
        parts.append('<section><h2>Research</h2>')
        parts.append('<div class="grid">')
        for klass, names in sorted((research.get("adoptions_by_class") or {}).items()):
            parts.append(f'<div class="card"><div class="k">{e(klass)}</div>'
                         f'<div class="v">{e(", ".join(names))}</div></div>')
        parts.append("</div>")
        questions = research.get("open_questions") or []
        if questions:
            parts.append(f"<h3>Open questions ({len(questions)})</h3><ul>")
            parts += [f"<li>{e(q)}</li>" for q in questions]
            parts.append("</ul>")
        parts.append("</section>")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DUM-E</title>
<style>
:root {{ color-scheme: light dark; --fg:#1a1a1a; --bg:#fbfbfa; --line:#e0ddd8;
         --muted:#63666A; --card:#fff; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --fg:#e8e6e3; --bg:#16181d; --line:#2a2e37; --muted:#9aa0a6; --card:#1d2027; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; padding:1.5rem; background:var(--bg); color:var(--fg);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
h1 {{ font-size:1.3rem; margin:0 0 .2rem }} h2 {{ font-size:1rem; margin:1.8rem 0 .6rem;
  text-transform:uppercase; letter-spacing:.06em; color:var(--muted) }}
h3 {{ font-size:.9rem; margin:1.2rem 0 .4rem }}
.sub {{ color:var(--muted); margin:0 0 1rem; font-size:.85rem }}
.banner {{ background:#D55E00; color:#fff; padding:.7rem 1rem; border-radius:6px;
  margin-bottom:1rem; font-weight:600 }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); gap:.6rem }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:6px; padding:.7rem .8rem }}
.k {{ font-size:.75rem; color:var(--muted); text-transform:uppercase; letter-spacing:.05em }}
.v {{ margin-top:.25rem }} .v.big {{ font-size:1.6rem; font-weight:700 }}
table {{ width:100%; border-collapse:collapse; margin-top:.5rem; display:block; overflow-x:auto }}
th {{ text-align:left; font-size:.72rem; text-transform:uppercase; letter-spacing:.05em;
  color:var(--muted); border-bottom:1px solid var(--line); padding:.35rem .6rem .35rem 0 }}
td {{ padding:.4rem .6rem .4rem 0; border-bottom:1px solid var(--line); vertical-align:top }}
.chip {{ display:inline-block; padding:.1rem .5rem; border-radius:99px; font-size:.75rem;
  font-weight:600; color:var(--c); border:1px solid var(--c); white-space:nowrap }}
.muted {{ color:var(--muted); font-size:.85rem }}
.mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.82rem }}
ul {{ margin:.3rem 0; padding-left:1.1rem }} li {{ margin:.2rem 0; color:var(--muted); font-size:.87rem }}
footer {{ margin-top:2rem; color:var(--muted); font-size:.78rem; border-top:1px solid var(--line); padding-top:.8rem }}
</style></head><body>
<h1>DUM-E</h1>
<p class="sub">DUM-E builds AETHRION. AETHRION does the science. ·
generated {e(data['generated_at'])} · refreshes every 10s</p>
{''.join(parts)}
<footer>Read-only. Every action lives in the command gateway, where it is
authenticated, classified, rate-limited and audited — a button here would be a
second, weaker way to do the same thing.</footer>
<script>setTimeout(()=>location.reload(), 10000)</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/status.json"):
            body = json.dumps(snapshot(), indent=2, default=str).encode()
            content = "application/json"
        else:
            body = render(snapshot()).encode()
            content = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the console for the harness
        pass


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Bound to localhost by default.

    The page shows candidate revisions, runtime identities and open findings.
    None of that should be one firewall rule away from the internet, and a
    read-only page is still a disclosure.
    """
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"operator view on http://{host}:{port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        server.server_close()
