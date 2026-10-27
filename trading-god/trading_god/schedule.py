"""Register the daily pre-market scan with Vibe-Trading's scheduler."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from trading_god.config import TEMPLATE_DIR, Settings, home_dir

JOB_ID = "trading-god-premarket"
JOB_TITLE = "Trading God pre-market scan (paper)"


def watchlist_path() -> Path:
    return home_dir() / "watchlist.txt"


def load_watchlist(path: Path | None = None) -> list[str]:
    """Tickers from ``watchlist.txt``: one per line or comma separated, ``#`` starts a comment."""
    for candidate in (path, watchlist_path(), TEMPLATE_DIR / "watchlist.txt"):
        if candidate is not None and candidate.is_file():
            tickers: list[str] = []
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0]
                tickers.extend(t.strip().upper() for t in line.split(",") if t.strip())
            return list(dict.fromkeys(tickers))
    return []


def render_prompt(settings: Settings, watchlist: list[str]) -> str:
    template = (TEMPLATE_DIR / "premarket_prompt.md").read_text(encoding="utf-8")
    tickers = ", ".join(watchlist) if watchlist else "(empty: screen large, liquid US stocks yourself)"
    return (
        template.replace("{watchlist}", tickers)
        .replace("{max_proposals}", str(settings.schedule.max_proposals_per_run))
        .strip()
    )


def job_payload(settings: Settings, watchlist: list[str]) -> dict[str, Any]:
    return {
        "id": JOB_ID,
        "title": JOB_TITLE,
        "prompt": render_prompt(settings, watchlist),
        "schedule": settings.schedule.cron,
        "timezone": settings.schedule.timezone,
    }


def register(settings: Settings, watchlist: list[str], *, api_key: str = "") -> dict[str, Any]:
    """Create or replace the job (the fixed id makes this safe to repeat on every start)."""
    url = settings.schedule.vibe_url.rstrip("/") + "/scheduled-runs"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = json.dumps(job_payload(settings, watchlist)).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - local Vibe-Trading server
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Vibe-Trading refused the schedule ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Vibe-Trading is not reachable at {settings.schedule.vibe_url}: {exc.reason}") from exc
