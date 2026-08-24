# updatejenkinsbot

A standalone Lark bot that **only** runs the Jenkins update flow, mirrored from
`osedutybot`. Paste a Jenkins update request → the bot logs into Jenkins, fills the
FPMS UAT branch-update form, replies with a screenshot and **Confirm / Cancel** buttons,
and triggers the build only after you click **Confirm**. Also supports `rebuild`,
the "create vpn" card, and `/warmstatus`.

It connects to Lark using **Receive events through a persistent connection** (long
connection / WebSocket) — no public HTTPS Request URL required.

## Layout

| File | Role |
|------|------|
| `main.py` | Entry point: Lark I/O + persistent-connection loop + dispatch to the engine |
| `jenkinsupdate.py` | The engine (Playwright form-fill, warm pool, sessions, cards) |
| `jenkinsupdateagent.py` | Natural-language request parser (optional LLM) |
| `updatemore.py` | Multi-environment `/updatemore` batching (optional) |
| `maintenance_mail.py` | Reply-email engine (IMAP search + SMTP reply-all in the original thread) |
| `maintenance.py` | Email parsing + Lark card helpers used by `maintenance_mail.py` / `updatemore.py` |
| `cpms_igo_uat_services.json` | Cached CPMS/IGO UAT service lists |
| `.env` | Credentials + config (not committed) |
| `deploy/updatejenkins.service` | systemd unit template |

`maintenance.py` + `maintenance_mail.py` are copied verbatim from `osedutybot`; they are
self-contained (stdlib + `requests`/`python-dotenv` only). `updatemore.py` imports them for the
`/replyupdateemail` flow.

`jenkinsupdate.py` calls back into `main.py` via `import main`; running `python main.py`
aliases `import main` to the running process so nothing is loaded twice.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env        # then edit .env
python main.py
```

On startup you should see:

```
[lark-ws] Persistent connection active (im.message + card.action.trigger). ...
```

## Lark developer console

1. **Credentials & Basic Info** → copy App ID / App Secret into `.env`.
2. **Event & Callback → Subscription mode** → choose **Receive events through
   persistent connection** (长连接).
3. Subscribe to events: **`im.message.receive_v1`** (Message received) and enable
   **card callbacks** (`card.action.trigger`) so the Confirm/Cancel buttons work.
4. **Permissions**: `im:message`, `im:message:send_as_bot`, `im:resource` (image upload),
   `im:message.reaction` (GotIt/DONE reactions).
5. Add the bot to the chat/group you will use, or DM it directly.

## Usage

- **DM** the bot, or **@mention** it in a group, with a request such as:

  ```
  /jenkinsupdate rc uat
  Branch: release/x.y
  Version: 1.2.3
  Services: svc-a, svc-b
  ```

- Click **Confirm** to trigger the build (or **Cancel**).
- `rebuild` / `rebuild again` — re-run the last update.
- `/warmstatus` — warm browser pool status: how many browsers are live, which are hot vs
  on-demand, and the idle-release window. See [Warm browser pool](#warm-browser-pool).
- `/testing` — **dry run**. Put it on the first line, above the update blocks, and the bot does
  everything a real run does *up to* the build: logs in, fills the form, verifies every value and
  sends you the screenshots — then stops. See [Dry runs](#dry-runs).

<a id="dry-runs"></a>
### Dry runs (`/testing`)

  ```
  /testing
  UPDATE FPMS UAT MASTER
  Branch: master
  Service: 3000,9000
  Version: v3.2.261

  UPDATE FPMS NT UAT MASTER
  Branch: master
  Service: admin-rollout
  Version: v4.2.65
  Email: FPMS v3.2.261 | NT v4.2.65 UPDATE PRODUCTION - CP (2026-08-21)
  ```

Every block is filled, verified and photographed in turn, and each gets a `🧪 TESTING` card with
its link, environment, branch, version, services, the verification verdict, and a **simulated**
done time. Multi-block runs advance on their own — there is no build to wait for.

What it will **not** do:

- **Never clicks Build.** Not "is asked not to" — the click itself refuses when the run is dry, so
  every path into Jenkins is closed, including the recovery that re-clicks Build to reload a stale
  Services list. The YES/NO buttons are left off the card entirely, because the gate they answer
  is already closed by then.
- **Never sends the email.** Instead the last card reports how many **To** and **Cc** addresses the
  `Email:` reply *would* have reached, plus the de-duplicated envelope count, resolved from the
  local `allemail.json` — no IMAP, no SMTP, no credentials needed. Refusal counts are not shown:
  they cannot be known without actually sending.
- **Never tells jenkinsbot a build happened**, arms a watchdog, or leaves email-batch state on the
  queue that a later real run could consume.

Notes:

- Screenshots are forced on for a dry run even if `JENKINSUPDATE_FORM_SCREENSHOT=0`; they are the
  only output it has. Real runs still honour that switch.
- A predicted build number is shown as *"would be #N"* — it is a `max+1` guess and no build will
  ever claim it.
- If a block fails verification, the card says a real run would have **refused** to build it, and
  the closing line names the blocks that failed. Later blocks still dry-run.
- If the **Services** checkbox list does not render, a dry run reloads, re-logs in and retries the
  fill. It skips the `Refresh pipeline` -> `Build` step that a real run uses there, so it never
  builds; if the list is genuinely unpublished the segment stops with that as its reason. Note
  that a REAL run hitting this silently runs an extra Jenkins build to republish the parameters.
  If it happens often, set `FPMS_STABLE_FILL=1` -- fast-fill mode clamps the
  `FPMS_SERVICES_APPEAR_MS` wait to 10s, and raising that variable on its own does nothing while
  the clamp is active.
- Because of that clamp, a dry run waits `FPMS_DRY_RUN_SERVICES_APPEAR_MS` (default **90s**) for
  the Services list instead of the live 10s. UnoChoice has been measured taking 24-31s to mount on
  FPMS_NT, and a dry run has no build queued behind it, so patience is free. The override can only
  lengthen the wait, never shorten it.
- If the list still never appears, `/testing` says so and names the fix: run **that one segment as
  a normal update once**. Only a real build republishes a job's parameter list, and a dry run
  cannot do it by definition.
- Two combinations are **refused** rather than half-honoured:
  - **VPN creation.** That flow clicks Build from a warm browser that never sees the dry-run flag,
    so a dry run there could not be made safe.
  - **`/testing` together with `skip build`.** They are opposites: `skip build` skips the Build
    click but still sends the real customer Reply-All.
- `/testing` will not start while a real update is still running in the same chat. The callbacks
  that drive a real multi-block run forward are not operator messages, so an overlapping dry run
  could colour a segment that belongs to the real run. Finish or cancel it first.
- A dry run occupies the same warm browser as a real update for the same job, so it will delay a
  genuine update running against that job.

<a id="warm-browser-pool"></a>
### Warm browser pool

A warm browser is a logged-in headless Chromium parked on a job's *build with parameters* page, so
an update only has to fill the form instead of launching + logging in (~20s). It is not free:
roughly **6 `chrome-headless-shell` processes and a Node driver, ~385 MB, each**.

The pool knows 29 Jenkins job URLs. It used to pre-warm **all** of them at startup and re-warm them
every 4 minutes forever — ~30 browsers, ~180 processes, ~11 GB, most of them for jobs nobody had
asked for. Now:

- only the jobs in `JU_WARM_HOT_URLS` are pre-warmed and held (empty = the 5-job default in
  `_JU_WARM_HOT_DEFAULT`, chosen from alias counts, bespoke service lists and test fixtures);
- any other known job launches on **first use** — one ~20s wait, which an `/update` run
  announces in chat (internal parameter-discovery calls just wait) — and is released again
  after `JU_WARM_IDLE_TTL_SEC` (default 1800s) of idleness;
- `/warmstatus` shows which are hot, which are lazy-and-live, and which are idle.

Steady state is ~6 browsers / ~36 processes instead of ~30 / ~180.

To go back to warming everything, no code change needed:

```
JU_WARM_HOT_URLS=*
JU_WARM_IDLE_TTL_SEC=0
JU_WARM_KEEPALIVE_SEC=240
```

See the `Playwright / warm browser pool` section of `.env.example` for every knob, including
`JU_WARM_CHROME_ARGS` and the switches that must never be passed.

- **Reply to the update email** — `replyupdateemail | {email title} | {pipeline/env} | {time}`
  makes the bot search the mailbox (`JENKINS_REPLY_IMAP_FOLDERS`) for the original request thread
  and **reply-all** that the build is done. Works in a group **without** an @mention (any sender),
  and also over HTTP: `POST /internal/reply-update-email` with JSON
  `{"chat_id","email_title","environment","when"}` (optional `X-Duty-Internal-Token` header when
  `DUTY_INTERNAL_TOKEN` is set). Requires `MAINTENANCE_MAIL_PASSWORD` (and the other
  `MAINTENANCE_MAIL_*` values — see `.env.example`) so the bot can reach IMAP/SMTP; without it the
  reply cannot be sent and the bot posts a manual-reply fallback card instead.

  The reply quotes the original underneath it using Lark Mail's own quote markup
  (`history-quote-wrapper` + `adit-html-block--collapsed`), so recipients see the collapsible
  **Show/Hide email thread** with the previous email inside — identical to a manual **Reply All**
  in the Lark Mail composer. It is sent as a single `text/html` part (a plain-text alternative
  makes Lark expand the quote as raw text). On a cache hit the original is re-read from IMAP by
  `Message-ID` just to build the quote; if that lookup fails the reply still goes out, as flat
  plain text. Set `JENKINS_REPLY_QUOTE_THREAD=0` to force the old plain-text reply.
- **Self-update:** `@Jenkins Update Bot git pull origin main and restart service` (or `/deploy`)
  → the bot runs `git pull origin main` in its repo, replies with the result, then restarts
  its own systemd unit so the new code takes effect. Also matches `git pull and restart`,
  `/gitpullrestart`, `拉代码重启`. Restricted to the open_ids in `DEPLOY_ALLOWED_OPEN_IDS`
  (empty = anyone who can @mention). The unit name comes from `UPDATEJENKINS_SERVICE`
  (default `updatejenkins`).

  Requirements on the server for this to work unattended:
  - The service user can run `systemctl restart <unit>` without a password (root does; a
    non-root user needs a sudoers/polkit rule).
  - `git pull` has credentials for the repo (cached Git credential helper, a token, or an
    SSH deploy key) if it is private, and no local edits to **tracked** files (`.env` and
    `jenkinsupdate.json` are gitignored, so they never conflict).

## Git

```bash
git init
git add -A
git commit -m "Initial standalone Jenkins update bot"
git branch -M main
git remote add origin https://github.com/mrcodestealer/jenkinsupdate.git
git push -u origin main
```

Afterwards: `git push origin main` / `git pull origin main`.

## Run as a service (systemd)

See `deploy/updatejenkins.service`. On the server:

```bash
sudo cp deploy/updatejenkins.service /etc/systemd/system/updatejenkins.service
# Edit WorkingDirectory / EnvironmentFile / ExecStart paths to match the server.
sudo systemctl daemon-reload
sudo systemctl enable --now updatejenkins
sudo systemctl status updatejenkins
journalctl -u updatejenkins -f          # live logs
sudo systemctl restart updatejenkins    # after a git pull
```
