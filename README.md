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
| `cpms_igo_uat_services.json` | Cached CPMS/IGO UAT service lists |
| `.env` | Credentials + config (not committed) |
| `deploy/updatejenkins.service` | systemd unit template |

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
- `/warmstatus` — show the warm browser pool status.

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
