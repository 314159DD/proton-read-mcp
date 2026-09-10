# proton-read-mcp

[![Tests](https://github.com/314159DD/proton-read-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/314159DD/proton-read-mcp/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)

A tiny **read-only** Proton Mail MCP server. It lets Claude (Desktop or Claude Code) read and search your Proton inbox live through Proton Bridge. Nothing else.

## Why this exists

You want Claude to *see* your mail, not run your mailbox. So this server is read-only by construction:

- No tools that send, reply, delete, move, flag, or modify anything. They don't exist in the code.
- Every fetch uses `BODY.PEEK`, so reading does **not** mark a message as seen.
- Folders are opened `readonly=True`.
- No local cache or database. It reads live over IMAP and stores nothing on disk. There is no decrypted copy of your mail sitting around.
- One dependency (`mcp`); everything else is the Python standard library.

Sending stays wherever you already do it. This server is only the eyes.

## Prerequisites

- **Proton Bridge** installed, signed in, and running (on the same machine, or on a server you can reach over SSH).
- **Python 3.10+**.
- Your Bridge IMAP host/port and the **Bridge password** (Proton Bridge → your account → Mailbox details). This is not your Proton login password.

## Install

```bash
git clone https://github.com/314159DD/proton-read-mcp.git
cd proton-read-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Verify Bridge connectivity before wiring up any MCP client:

```bash
PROTONMAIL_USERNAME=you@pm.me \
PROTONMAIL_PASSWORD_FILE=~/.proton-bridge-pass \
python server.py --check
```

You should see a folder list and an INBOX count. `--check` is read-only (folder
list + counts only) and never starts the server.

Put the Bridge password in a file (so it never sits in a config or shell history):

```bash
printf '%s' 'your-bridge-password' > ~/.proton-bridge-pass
chmod 600 ~/.proton-bridge-pass
```

## Topology

Bridge runs on the **VPS** and listens on `127.0.0.1:1143`. Two ways to reach it:

### A) Claude Code on the VPS (direct)

Bridge is already on localhost, so just register the server:

```bash
claude mcp add proton-read -s user -- \
  python /path/to/proton-read-mcp/server.py
```

Set the env for that entry (in `~/.claude.json` under the `proton-read` server, or export before launching):

```
PROTONMAIL_USERNAME=you@pm.me
PROTONMAIL_PASSWORD_FILE=/home/you/.proton-bridge-pass
PROTONMAIL_IMAP_HOST=127.0.0.1
PROTONMAIL_IMAP_PORT=1143
```

### B) Claude Desktop on your Mac (via SSH tunnel)

The server has no remote URL on purpose. Forward the VPS Bridge IMAP port to your Mac's localhost, then point the local server at it. Keep this tunnel running while you use it (autossh or a LaunchAgent is handy):

```bash
ssh -N -L 1143:127.0.0.1:1143 you@your-vps
```

Then add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "proton-read": {
      "command": "/ABSOLUTE/PATH/proton-read-mcp/.venv/bin/python",
      "args": ["/ABSOLUTE/PATH/proton-read-mcp/server.py"],
      "env": {
        "PROTONMAIL_USERNAME": "you@pm.me",
        "PROTONMAIL_PASSWORD_FILE": "/Users/you/.proton-bridge-pass",
        "PROTONMAIL_IMAP_HOST": "127.0.0.1",
        "PROTONMAIL_IMAP_PORT": "1143"
      }
    }
  }
}
```

Restart Claude Desktop, keep Bridge (on the VPS) and the tunnel up, then check `+` → Connectors → `proton-read`.

> Running it on the Mac means the mail flows (decrypted by Bridge) over the SSH tunnel to the Mac process. The SSH tunnel is encrypted and not publicly exposed. If you want the Mac to stay completely mail-free, skip path B and just use Claude Code on the VPS (path A).

### C) Same machine (Bridge local, e.g. a Windows or Mac desktop)

If you run Bridge and Claude on the *same* box, there is no tunnel - Bridge already
listens on `127.0.0.1`, so just point the server at it. This is the simplest setup.

1. Install Proton Bridge and sign in (on Windows: `winget install Proton.ProtonMailBridge`).
2. Bridge → your account → **Mailbox details**. Note the **IMAP port** and the
   per-device **Bridge password**.
   - **Port gotcha:** Bridge's default IMAP port is `1143`, but if something else
     already holds `1143` Bridge silently falls back to the next free port (e.g.
     `1144`). Always use the port Bridge actually reports, and set
     `PROTONMAIL_IMAP_PORT` to match.
3. Store the Bridge password in a locked-down file outside the repo and verify:

   ```bash
   python server.py --check
   ```

**Claude Code (any OS):**

```bash
claude mcp add proton-read -s user \
  -e PROTONMAIL_USERNAME=you@pm.me \
  -e PROTONMAIL_PASSWORD_FILE=/path/to/.proton-bridge-pass \
  -e PROTONMAIL_IMAP_HOST=127.0.0.1 \
  -e PROTONMAIL_IMAP_PORT=1144 \
  -- /path/to/.venv/bin/python /path/to/server.py
```

**Claude Desktop on Windows - config path:** the normal location is
`%APPDATA%\Claude\claude_desktop_config.json`. But the **Microsoft Store (MSIX)**
build virtualizes AppData, so its real config lives under:

```
%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json
```

Add the same `mcpServers` block shown in path B (use the venv `python.exe` and the
absolute `server.py` path with `\\`-escaped backslashes), then **fully quit Claude
Desktop from the system tray** (closing the window is not enough) and relaunch. The
server appears under **Settings → Developer**, not in the `+` → Connectors list
(that list is for remote/OAuth connectors; local stdio servers show under Developer).

## Tools

| Tool | What it does |
| --- | --- |
| `list_folders()` | List all folders and labels. |
| `list_recent(folder="INBOX", limit=20)` | Newest emails, lightweight metadata. |
| `search_emails(query, folder="INBOX", limit=20)` | Free-text search over a folder. |
| `read_email(email_id, folder="INBOX")` | Full headers + plain-text body + attachment names. |
| `unread_count(folder="INBOX")` | Unread and total counts. |

`list_recent` and `search_emails` return an `id` per message; pass it to `read_email`.

## Notes & limits

- Search matching is ASCII-oriented (IMAP `TEXT`). For names with umlauts, try a substring like `Mueller` or search by company domain.
- It opens a fresh IMAP connection per call. Simple and stateless; fine for interactive use, not for high-frequency polling.
- No classification or scoring is built in. Feed `list_recent` / `search_emails` output into your own logic if you need that.

## License

MIT - see [LICENSE](LICENSE).
