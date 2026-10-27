import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from trading_god.config import ScheduleSettings, Settings
from trading_god.schedule import JOB_ID, job_payload, load_watchlist, register, render_prompt


def test_watchlist_accepts_lines_commas_and_comments(tmp_path):
    path = tmp_path / "watchlist.txt"
    path.write_text("# my list\nspy, qqq\nAAPL  # Apple\n\naapl\nMSFT,\n", encoding="utf-8")
    assert load_watchlist(path) == ["SPY", "QQQ", "AAPL", "MSFT"]


def test_bundled_watchlist_is_used_until_there_is_a_user_copy():
    assert load_watchlist()[:2] == ["SPY", "QQQ"]


def test_prompt_fills_every_placeholder():
    prompt = render_prompt(Settings(), ["SPY", "AAPL"])
    assert "SPY, AAPL" in prompt
    assert "at most 3 new buys" in prompt
    assert "trading-god-desk" in prompt
    assert "{" not in prompt


def test_job_payload_uses_the_schedule_settings():
    settings = replace(Settings(), schedule=ScheduleSettings(cron="0 9 * * 1-5", timezone="America/Chicago"))
    payload = job_payload(settings, ["AAPL"])
    assert (payload["id"], payload["schedule"], payload["timezone"]) == (JOB_ID, "0 9 * * 1-5", "America/Chicago")


class _Recorder(BaseHTTPRequestHandler):
    calls: list = []
    status = 201

    def do_POST(self):  # noqa: N802 - http.server API
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).calls.append((self.path, self.headers.get("Authorization"), json.loads(body)))
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"id": JOB_ID, "title": "Trading God pre-market scan (paper)"}).encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def vibe_server():
    _Recorder.calls = []
    _Recorder.status = 201
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_register_posts_the_job(vibe_server):
    settings = replace(Settings(), schedule=ScheduleSettings(vibe_url=vibe_server))
    job = register(settings, ["AAPL"], api_key="secret")
    assert job["id"] == JOB_ID
    ((path, auth, body),) = _Recorder.calls
    assert (path, auth, body["id"]) == ("/scheduled-runs", "Bearer secret", JOB_ID)


def test_register_explains_failures(vibe_server):
    _Recorder.status = 422
    settings = replace(Settings(), schedule=ScheduleSettings(vibe_url=vibe_server))
    with pytest.raises(RuntimeError, match="refused the schedule"):
        register(settings, ["AAPL"])
    closed = replace(Settings(), schedule=ScheduleSettings(vibe_url="http://127.0.0.1:1"))
    with pytest.raises(RuntimeError, match="not reachable"):
        register(closed, ["AAPL"])
