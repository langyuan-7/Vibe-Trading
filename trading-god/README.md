# Trading God

A research agent for US stocks that runs on your own Windows PC and trades a
**moomoo paper account only**. Every morning it reads the market and your watch list,
applies five investor playbooks, and proposes a few orders. Nothing is placed until
you approve it on a local web page.

It builds on four repositories: [Vibe-Trading](../README.md) is the agent and
scheduler, Claude Code is the model (through your own Claude login), OpenBB and
Vibe-Trading's loaders supply the data, and
[investorskills](https://github.com/langyuan-7/investorskills) supplies the playbooks.

```
 8:35 a.m. New York time, weekdays          (Vibe-Trading scheduler)
   │
   │  Claude Code + playbooks + OpenBB and Vibe-Trading data tools
   ▼
 Agent proposes an order ──► desk risk checks ──► desk page: you approve or reject
                                 (refused if a        │
                                  limit is broken)    │ approve: every check runs again
                                                      ▼ against the live account
                                               moomoo OpenD ──► US paper account
```

## What is in phase 1

- **The approval desk** (`trading_god/`): an order journal, hard risk limits, a
  moomoo paper-account adapter, and the desk page at http://127.0.0.1:8765.
- **Desk tools for the agent**: `get_portfolio`, `propose_order`, `list_proposals`
  and `withdraw_proposal`. There is no tool to approve, place or cancel an order.
- **The playbooks**: `marks-cycles` for the market regime, `oneil-canslim` and
  `minervini-vcp` for entries, and `buffett` and `lynch-growth` for business quality.
  The `trading-god-desk` skill ties them into one process. It checks the regime,
  wants two playbooks to agree, sizes each order by the loss at its stop, reviews
  exits, and reads your rejection notes before proposing again.
- **The daily scan**: registered with Vibe-Trading's scheduler for 8:35 a.m. New York time.
- **Windows scripts**: `setup\install.ps1`, `start.ps1`, `stop.ps1`, `update.ps1`, and `shortcuts.ps1` for the desktop icons.

## Safety rails

- **Paper only.** The adapter sends every order with `TrdEnv.SIMULATE`, picks its
  account only from OpenD's paper accounts, and never calls `unlock_trade`, which a
  live order needs. A test fails if that code changes.
- **A person approves every order, except a stop.** The agent can only propose.
  Vibe-Trading's own broker tools (`trading_*`) are hidden from the model. The one
  order nobody approves is the stop-loss sell: while the desk runs, it checks every
  position bought through it once a minute, and in market hours sells it at market as
  soon as the price is at or below the stop you approved with the buy. A gap down can
  fill below the stop. The desk page and Telegram tell you when it happens, and
  `[exits] auto_stop = false` in `desk.toml` turns it off. The PC has to be awake with
  Trading God running: the stop is not an order waiting at moomoo.
- **Swing trades carry their exits.** A swing buy (2 to 10 trading days, from the
  `darvas-box` or `livermore` playbook) must come with a stop, a target at least 1.5
  times as far as the stop, and a number of days. The card says that approving it
  approves those exits, and the desk then sells at whichever comes first: the stop, the
  target, or 15:45 New York on the last day. `[swing]` in `desk.toml` sets the limits
  and turns the automatic target and time exits off.
- **Stops only go up.** For Minervini and O'Neil buys, the desk trails the stop once a
  day at the highest close since the buy minus 3 average daily ranges (ATR), so a calm
  stock trails close and a jumpy one gets room. The agent can also raise a stop to a
  level its playbook names, such as under a new higher base or the 50-day line, at
  least 2% below the price. Nothing can lower a stop. Buffett, Lynch and Marks buys keep
  the stop they were approved with; the morning scan judges them on the thesis.
- **Hard limits, checked twice.** The desk checks each order when it is proposed, and
  again against the live account when you approve it. The defaults, as a share of
  equity:
  - at most 5% in one order and 10% in one stock
  - at least 10% kept in cash
  - at most 5 buys a day
  - every buy needs a stop below its entry
  - no stocks under $5, no margin, no short selling, and no options or crypto

  Buy orders that have not filled yet count against cash and position size.
  Sells are only checked for shorting, so a stop-loss exit is never blocked.
- **Local only.** The desk page answers only on 127.0.0.1. It checks the Host and
  Origin headers, and its forms carry a token, so another website cannot approve an
  order for you.

## Honest limits

- This is an experiment. Paper fills are kinder than real ones: there is no slippage
  and no partial fills from thin liquidity, so paper results will look better than
  real money would.
- The agent can be wrong with confidence. Read each thesis. Your rejection notes are
  how it learns, so write them.
- The scan runs only while the PC is awake, with OpenD running and logged in, at
  8:35 a.m. New York time.
- Claude Code uses your plan's usage limits. One scan is many model calls. The
  default model is Sonnet. `opus` analyses more deeply but uses your plan faster.
- OpenBB's free sources (Nasdaq, SEC, Cboe and others) can be slow or rate limited.
  When they fail, the agent says so in its report and falls back to Vibe-Trading's
  own data tools.

## Install (Windows 10 or 11)

You need moomoo OpenD running and logged in, with Paper Trading opened once for
US stocks in the moomoo app.

1. If Git is not installed, install it: `winget install --id Git.Git -e`, then open a
   new PowerShell window.
2. Get the code:

   ```powershell
   git clone https://github.com/langyuan-7/Vibe-Trading.git
   cd Vibe-Trading
   git checkout claude/project-thread-empa9s   # until this branch is merged into main
   ```

3. Install. This asks before installing uv, Node.js and Claude Code if they are missing:

   ```powershell
   powershell -ExecutionPolicy Bypass -File trading-god\setup\install.ps1
   ```

   It creates two Python environments under `%USERPROFILE%\.trading-god\venvs`, one
   for Vibe-Trading and one for OpenBB, which needs newer libraries. It builds the
   Vibe-Trading web page and runs `tg-desk setup`, which:
   - points Vibe-Trading at Claude Code, the desk and OpenBB
     (`%USERPROFILE%\.vibe-trading\.env` and `agent.json`, backed up first)
   - installs the six skills into `%USERPROFILE%\.vibe-trading\skills\user`
   - copies `desk.toml` and `watchlist.txt` into `%USERPROFILE%\.trading-god`

   It also puts three icons on the desktop: **Start Trading God**, **Stop Trading God**
   and **Update Trading God**.

4. Sign in to Claude Code once, if you have not: `claude auth login`.
5. Check moomoo: `%USERPROFILE%\.trading-god\venvs\vibe\Scripts\tg-desk.exe check`
   should show your US paper account and its balance.

## Daily use

Double-click **Start Trading God** on the desktop. If moomoo OpenD is not running, it
starts OpenD from its shortcut and waits up to two minutes for you to log in. Then it
starts OpenBB, the desk and Vibe-Trading, each in a minimized window, registers the
8:35 a.m. scan and opens two pages:

- **Desk page** at http://127.0.0.1:8765. Proposals wait here. Each card shows the
  order, the loss if the stop is hit, the thesis, the playbooks and the risk checks.
  Add a note and approve or reject. Proposals expire after 12 hours, but never before
  the next market close, so an evening scan can be approved the next day.
- **Vibe-Trading** at http://127.0.0.1:8899. Read the scan reports and chat with the
  agent. To run a scan now, paste the output of `tg-desk prompt` into a new chat.

**Stop Trading God** stops the three services and leaves OpenD alone. **Update Trading
God** pulls the latest code, reinstalls dependencies or rebuilds the web page only when
they changed, and restarts everything. Each icon's window closes by itself when all
went well, and stays open on an error or a warning so you can read it.

The icons run the scripts in `trading-god\setup`, which also work from PowerShell:
`start.ps1`, `stop.ps1` and `update.ps1`, each run as
`powershell -ExecutionPolicy Bypass -File trading-god\setup\<name>.ps1`. If the icons
are missing, `shortcuts.ps1` puts them back. `shortcuts.ps1 -AtLogin` also starts
Trading God when you sign in to Windows.

`tg-desk` (in `%USERPROFILE%\.trading-god\venvs\vibe\Scripts`) does the same from a
terminal:

| Command | What it does |
| --- | --- |
| `tg-desk list --status pending` | Proposals waiting for you |
| `tg-desk approve 12 --note "ok"` | Show #12, ask to confirm, then place it on the paper account |
| `tg-desk reject 12 --note "too extended"` | Reject #12; the agent reads the note next time |
| `tg-desk portfolio` | Account, positions, limits in dollars, buys left today |
| `tg-desk check` | Is OpenD up, which paper account is in use |
| `tg-desk prompt` | The scan prompt, to run a scan by hand |
| `tg-desk schedule` | Register the scan again, for example after changing the watch list |
| `tg-desk skills` | Refresh the agent's Trading God skill after a `git pull` |

## Settings

Edit the copies in `%USERPROFILE%\.trading-god`, not the templates in `config/`.
Restart with `stop.ps1` and `start.ps1` afterwards.

- `desk.toml` holds the risk limits, the proposal expiry, the scan time, how many
  buys one scan may propose (3), and an optional Telegram alert for new proposals.
  Every setting is commented.
- `watchlist.txt` holds the tickers the scan looks at.
- The playbooks are listed in `config/playbooks.txt`. Rerun `install.ps1` after
  changing it.
- The model: rerun `install.ps1 -Model opus` to use Opus.

## Troubleshooting

- **"moomoo OpenD is not listening"**: start OpenD and log in. It must stay open.
- **"OpenD reports no US paper stock account"**: open Paper Trading for US stocks
  once in the moomoo app.
- **No price for a symbol**: the desk tries moomoo first, then Yahoo Finance. moomoo
  quotes need a US quote entitlement on the account.
- **The agent does nothing**: in Vibe-Trading, check that the provider is
  `claude-cli`, and run `claude auth status` in PowerShell.
- **OpenBB window shows errors on start**: its first start takes up to a minute.
  The agent works without it, with fewer data sources.
- **A proposal stuck at "placing"**: the desk stopped while sending the order. Look
  in moomoo's order list for the remark `trading-god #<id>` before you approve
  anything like it again.

## Next (phase 2)

- Wrap TauricResearch's multi-agent debate and Agent-Reach's web and social search as tools.
- Keep a scorecard of every placed trade against its stop and target, broken down by
  playbook, and feed it back into the skill. This is the self-learning loop.
- Move from approving every order to automatic orders within tighter limits, once
  the paper record supports it.

## Development

```bash
pip install -e "trading-god[test]"     # into an environment that has Vibe-Trading installed
pytest trading-god/tests
```

| Path | Contents |
| --- | --- |
| `trading_god/risk.py` | The hard limits, as pure functions |
| `trading_god/desk.py` | Propose, approve, reject; the only path to the broker |
| `trading_god/broker.py` | moomoo paper adapter (futu-api SDK and OpenD) |
| `trading_god/store.py` | SQLite journal at `%USERPROFILE%\.trading-god\desk.db` |
| `trading_god/web.py` | The desk page |
| `trading_god/mcp_server.py` | The agent's desk tools (stdio MCP server) |
| `trading_god/setup_vibe.py` | Wires Vibe-Trading, OpenBB, moomoo and the skills |
| `skills/trading-god-desk/` | The agent's process |
| `config/` | Templates for `desk.toml`, the watch list, the playbook list and the scan prompt |
