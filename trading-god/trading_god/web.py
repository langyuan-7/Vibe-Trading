"""The desk page: where a person approves or rejects the agent's proposals.

It listens on 127.0.0.1 only. Every approve or reject form carries a token
minted when the server starts, and requests with a foreign Host or Origin
header are refused, so another website open in the browser cannot approve an
order behind the person's back.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timezone
from html import escape
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from trading_god import store as st
from trading_god.broker import BrokerError
from trading_god.desk import Desk, describe

try:
    _NEW_YORK: ZoneInfo | None = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    _NEW_YORK = None


def _when(value: str | None) -> str:
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return escape(value)
    if _NEW_YORK is None:
        return moment.strftime("%b %d %H:%M UTC")
    return moment.astimezone(_NEW_YORK).strftime("%b %d %H:%M ET")


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


_STYLE = """
:root { --bg:#f6f7f9; --card:#fff; --ink:#1b1f24; --muted:#5b6470; --line:#d9dde3;
        --good:#1a7f37; --bad:#c62828; --accent:#0b57d0; --warn:#9a6700; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1115; --card:#181b21; --ink:#e7e9ee; --muted:#9aa3ae; --line:#2b3039;
          --good:#3fb950; --bad:#f85149; --accent:#6ea8fe; --warn:#d29922; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 20px 16px 60px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 28px 0 10px; }
.sub { color: var(--muted); margin: 0 0 16px; }
.banner { padding: 10px 14px; border-radius: 8px; background: var(--card); border: 1px solid var(--line); margin: 12px 0; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.stat { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; }
.stat b { display:block; font-size: 19px; }
.stat span { color: var(--muted); font-size: 13px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px; margin: 12px 0; }
.card h3 { margin: 0 0 6px; font-size: 17px; }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 10px; }
.thesis { white-space: pre-wrap; margin: 10px 0; }
details.checks { margin: 8px 0 12px; font-size: 13px; }
details.checks summary { cursor: pointer; color: var(--muted); }
details.checks ul { list-style: none; padding: 0; margin: 6px 0 0; }
details.checks li { padding: 2px 0; }
.risk { margin: 6px 0; }
.ok { color: var(--good); } .no { color: var(--bad); }
textarea { width: 100%; min-height: 54px; border-radius: 8px; border: 1px solid var(--line);
           background: var(--bg); color: var(--ink); padding: 8px; font: inherit; }
.actions { display: flex; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
button { font: inherit; border-radius: 8px; padding: 8px 18px; cursor: pointer; border: 1px solid var(--line); }
button.approve { background: var(--good); color: #fff; border-color: var(--good); }
button.reject { background: transparent; color: var(--bad); border-color: var(--bad); }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line);
        border-radius: 10px; font-size: 13px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
.status { font-weight: 600; }
.status.placed { color: var(--good); } .status.rejected, .status.failed, .status.blocked, .status.refused { color: var(--bad); }
.status.pending, .status.placing { color: var(--warn); }
.empty, .note { color: var(--muted); }
"""

_REFRESH_SCRIPT = """
<script>
setInterval(function () {
  var busy = Array.from(document.querySelectorAll('textarea')).some(function (t) {
    return t.value.trim() !== '' || document.activeElement === t;
  });
  if (!busy) { window.location.replace('/'); }
}, 60000);
</script>
"""


def _checks_html(report: dict[str, Any]) -> str:
    checks = report.get("checks", [])
    if not checks:
        return ""
    items = []
    for check in checks:
        mark = '<span class="ok">&#10003;</span>' if check.get("passed") else '<span class="no">&#10007;</span>'
        items.append(f"<li>{mark} {escape(str(check.get('detail', check.get('name', ''))))}</li>")
    failed = sum(1 for check in checks if not check.get("passed"))
    summary = (
        f'<span class="no">&#10007; {failed} of {len(checks)} risk checks failed</span>'
        if failed
        else f'<span class="ok">&#10003;</span> All {len(checks)} risk checks passed when proposed'
    )
    return f'<details class="checks"{" open" if failed else ""}><summary>{summary}</summary><ul>{"".join(items)}</ul></details>'


def _risk_line(p: st.Proposal) -> str:
    """What the person stands to lose if the stop is hit, the number to weigh before approving."""
    entry = p.limit_price or p.ref_price
    if p.side != "buy" or not entry or not p.stop_loss or p.stop_loss >= entry:
        return ""
    loss = (entry - p.stop_loss) * p.quantity
    return f"Loss if stopped out: <b>{_money(loss)}</b> ({100.0 * (entry - p.stop_loss) / entry:.1f}% below entry)"


def _pending_card(p: st.Proposal, token: str) -> str:
    extras = [f"Playbook: {escape(p.playbook)}"]
    if p.stop_loss:
        extras.append(f"Stop {_money(p.stop_loss)}")
    if p.take_profit:
        extras.append(f"Target {_money(p.take_profit)}")
    if p.horizon:
        extras.append(f"Horizon: {escape(p.horizon)}")
    if p.confidence:
        extras.append(f"Confidence: {escape(p.confidence)}")
    return f"""
<div class="card">
  <h3>{escape(describe(p))}</h3>
  <div class="meta">Proposed {_when(p.created_at)} &middot; expires {_when(p.expires_at)} &middot; last price when proposed {_money(p.ref_price)}</div>
  <div class="meta">{" &middot; ".join(extras)}</div>
  {f'<div class="risk">{_risk_line(p)}</div>' if _risk_line(p) else ""}
  <div class="thesis">{escape(p.thesis)}</div>
  {_checks_html(p.risk_report)}
  <form method="post">
    <input type="hidden" name="token" value="{escape(token)}">
    <textarea name="note" placeholder="Optional note for the agent (it reads this next time), e.g. 'too extended, wait for a pullback'"></textarea>
    <div class="actions">
      <button class="approve" formaction="/proposals/{p.id}/approve"
              onclick="return confirm('Place this order on the moomoo PAPER account?')">Approve and place</button>
      <button class="reject" formaction="/proposals/{p.id}/reject">Reject</button>
    </div>
  </form>
</div>"""


def _portfolio_html(portfolio: dict[str, Any]) -> str:
    if portfolio.get("status") != "ok":
        return f'<div class="banner no">Cannot read the paper account: {escape(str(portfolio.get("error", "")))}</div>'
    account = portfolio["account"]
    today = portfolio["today"]
    stats = [
        (_money(account["equity"]), "Equity (paper)"),
        (_money(account["cash"]), "Cash"),
        (f"{account['invested_pct'] or 0:.1f}%", "Invested"),
        (f"{today['buys_left']}", "Buys left today"),
        (f"{portfolio['pending_proposals']}", "Waiting for you"),
    ]
    stat_html = "".join(f'<div class="stat"><b>{escape(v)}</b><span>{escape(k)}</span></div>' for v, k in stats)
    row_html = []
    for p in portfolio["positions"]:
        pl = "" if p["pl_pct"] is None else f"{p['pl_pct']:.1f}%"
        stop = "none" if p.get("stop_loss") is None else _money(p["stop_loss"])
        reason = str(p.get("stop_reason") or "")
        if reason.startswith("trailing"):
            stop += f' <small title="{escape(reason)}">trailing</small>'
        elif reason.startswith("raised"):
            stop += f' <small title="{escape(reason)}">raised</small>'
        row_html.append(
            f"<tr><td>{escape(p['symbol'])}</td><td>{escape(p['name'])}</td><td>{p['quantity']:g}</td>"
            f"<td>{_money(p['market_value'])}</td><td>{(p['weight_pct'] or 0):.1f}%</td><td>{pl}</td>"
            f"<td>{stop}</td></tr>"
        )
    rows = "".join(row_html)
    table = (
        f'<div class="table-wrap"><table><tr><th>Symbol</th><th>Name</th><th>Shares</th><th>Value</th>'
        f"<th>Weight</th><th>P/L</th><th>Stop</th></tr>{rows}</table></div>"
        if rows
        else '<p class="empty">No open positions.</p>'
    )
    note = (
        '<p class="note">A position that falls to its stop in market hours is sold at market, '
        "without asking. Stops only move up: trailing ones follow the highest close. "
        "Positions bought outside the desk have no stop.</p>"
        if portfolio.get("auto_stop")
        else ""
    )
    return f'<div class="stats">{stat_html}</div><h2>Positions</h2>{table}{note}'


def _history_html(rows: list[st.Proposal]) -> str:
    if not rows:
        return '<p class="empty">Nothing yet.</p>'
    body = "".join(
        f"<tr><td>{_when(p.created_at)}</td><td>{escape(describe(p))}</td>"
        f'<td class="status {escape(p.status)}">{escape(p.status)}</td>'
        f"<td>{escape(p.decision_note or '')}{'<br>' if p.decision_note and p.error else ''}{escape(p.error or '')}</td>"
        f"<td>{escape(p.broker_order_id or '')}</td></tr>"
        for p in rows
    )
    return (
        '<div class="table-wrap"><table><tr><th>Proposed</th><th>Order</th><th>Status</th><th>Note</th>'
        f"<th>moomoo order</th></tr>{body}</table></div>"
    )


def _orders_html(orders: list[dict[str, Any]]) -> str:
    if not orders:
        return '<p class="empty">No paper orders yet.</p>'
    body = "".join(
        f"<tr><td>{escape(str(o.get('create_time', '')))}</td><td>{escape(str(o.get('code', '')))}</td>"
        f"<td>{escape(str(o.get('trd_side', '')))}</td><td>{escape(str(o.get('qty', '')))}</td>"
        f"<td>{escape(str(o.get('price', '')))}</td><td>{escape(str(o.get('order_status', '')))}</td>"
        f"<td>{escape(str(o.get('dealt_qty', '')))} @ {escape(str(o.get('dealt_avg_price', '')))}</td></tr>"
        for o in orders
    )
    return (
        '<div class="table-wrap"><table><tr><th>Time</th><th>Code</th><th>Side</th><th>Qty</th><th>Price</th>'
        f"<th>Status</th><th>Filled</th></tr>{body}</table></div>"
    )


def create_app(desk: Desk) -> FastAPI:
    """Build the desk page around one :class:`Desk`."""
    token = secrets.token_urlsafe(32)
    port = desk.settings.web.port
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"{desk.settings.web.host}:{port}"}
    allowed_origins = {f"http://{host}" for host in allowed_hosts}
    app = FastAPI(title="Trading God desk", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return PlainTextResponse("This page only answers on 127.0.0.1.", status_code=403)
        origin = request.headers.get("origin")
        if request.method != "GET" and origin is not None and origin not in allowed_origins:
            return PlainTextResponse("Cross-site request refused.", status_code=403)
        return await call_next(request)

    def _back(message: str) -> RedirectResponse:
        return RedirectResponse(f"/?msg={quote(message)}", status_code=303)

    def _token_ok(value: str) -> bool:
        return hmac.compare_digest(value.encode("utf-8"), token.encode("utf-8"))

    @app.get("/", response_class=HTMLResponse)
    def index(msg: str = "") -> str:
        desk.expire_stale()
        pending = desk.store.list([st.PENDING], limit=50)
        history = [p for p in desk.store.list(None, limit=40) if p.status != st.PENDING]
        try:
            portfolio = desk.portfolio()
        except BrokerError as exc:
            portfolio = {"status": "error", "error": str(exc)}
        try:
            orders = desk.broker.recent_orders(10) if portfolio.get("status") == "ok" else []
        except BrokerError:
            orders = []
        banner = f'<div class="banner">{escape(msg)}</div>' if msg else ""
        cards = "".join(_pending_card(p, token) for p in pending) or (
            '<p class="empty">Nothing waiting. The agent\'s proposals show up here.</p>'
        )
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading God desk</title><style>{_STYLE}</style></head>
<body><main>
<h1>Trading God desk</h1>
<p class="sub">moomoo <b>paper</b> account &middot; every order needs your approval &middot; updated {_when(datetime.now(timezone.utc).isoformat())}</p>
{banner}
{_portfolio_html(portfolio)}
<h2>Waiting for your decision ({len(pending)})</h2>
{cards}
<h2>Recent decisions</h2>
{_history_html(history)}
<h2>moomoo paper orders</h2>
{_orders_html(orders)}
</main>{_REFRESH_SCRIPT}</body></html>"""

    @app.post("/proposals/{proposal_id}/approve")
    def approve(proposal_id: int, token: str = Form(""), note: str = Form("")) -> RedirectResponse:
        if not _token_ok(token):
            return _back("This page was out of date, so nothing happened. Check the proposal and try again.")
        result = desk.approve(proposal_id, note)
        if result["status"] == st.PLACED:
            return _back(f"Placed {result['summary']} on the paper account (moomoo order {result['order_id']}).")
        detail = "; ".join(result.get("problems", [])) or result.get("error", "")
        return _back(f"Proposal #{proposal_id} was not placed ({result['status']}): {detail}")

    @app.post("/proposals/{proposal_id}/reject")
    def reject(proposal_id: int, token: str = Form(""), note: str = Form("")) -> RedirectResponse:
        if not _token_ok(token):
            return _back("This page was out of date, so nothing happened. Check the proposal and try again.")
        result = desk.reject(proposal_id, note)
        if result["status"] == st.REJECTED:
            return _back(f"Rejected proposal #{proposal_id}.")
        return _back(result.get("error", "Nothing changed."))

    return app
