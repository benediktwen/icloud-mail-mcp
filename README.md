# icloud-mail-mcp

iCloud Mail MCP server. Exposes five tools — `search_emails`, `get_email`,
`list_labels`, `label_email`, `mark_read` — over Streamable HTTP with GitHub
OAuth authentication.

Follows the same architecture as `alpaca-mcp`, `icloud-calendar-mcp`, and
`garmin-connect-mcp`: Python + FastMCP, Docker on Render free tier, GitHub
OAuth (single-user), Upstash Redis for token persistence across cold starts.

---

## Tools

| Tool | Description |
|---|---|
| `search_emails` | Search by query, date range, unread flag. Returns `{id, subject, from, date, snippet}` |
| `get_email` | Fetch full message body by UID |
| `list_labels` | List all IMAP folders / labels |
| `label_email` | Move a message to a folder |
| `mark_read` | Mark a message as read |

---

## Prerequisites

- **iCloud App-Specific Password** — generate at [appleid.apple.com](https://appleid.apple.com) → Sign-In & Security → App-Specific Passwords. Never use your main Apple ID password.
- **GitHub OAuth App** — create at github.com/settings/developers. Set the callback URL to `https://<render-url>/auth/callback`.
- **Upstash Redis** — the same database used by the other MCPs works fine (share `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`).

---

## Deploy to Render

### 1. Create the GitHub repository

Push this directory to a new repo, e.g. `benediktwen/icloud-mail-mcp`.

```bash
git init
git add .
git commit -m "init: icloud-mail-mcp"
git remote add origin git@github.com:benediktwen/icloud-mail-mcp.git
git push -u origin main
```

Default branch must be `main`.

### 2. Create a Render Web Service

1. New → Web Service → connect the `icloud-mail-mcp` repo
2. Runtime: **Docker** (Render auto-detects the `Dockerfile`)
3. Instance type: **Free**

### 3. Set environment variables in Render dashboard

| Variable | Value |
|---|---|
| `ICLOUD_EMAIL` | `benedikt@wendlinger.me` (or whichever iCloud address) |
| `ICLOUD_APP_PASSWORD` | App-specific password from appleid.apple.com |
| `GITHUB_CLIENT_ID` | GitHub OAuth App client ID |
| `GITHUB_CLIENT_SECRET` | GitHub OAuth App client secret |
| `SERVER_URL` | `https://<your-render-url>.onrender.com` |
| `UPSTASH_REDIS_REST_URL` | Upstash REST URL |
| `UPSTASH_REDIS_REST_TOKEN` | Upstash REST token |
| `GITHUB_ALLOWED_USER` | `benediktwen` (already set in render.yaml) |

> `TOKEN_STORE_KEY` defaults to `icloud-mail-mcp`. Override if sharing a Redis DB with other MCPs and you need a different namespace (the other MCPs use `alpaca-mcp`, `icloud-calendar-mcp`, etc.).

### 4. Update GitHub OAuth App callback URL

Set the Authorization callback URL to:

```
https://<your-render-url>.onrender.com/auth/callback
```

### 5. Verify

Once deployed, the MCP endpoint is:

```
https://<your-render-url>.onrender.com/mcp
```

Add it as a connector in Claude Code (or any MCP client). On first use you will be redirected to GitHub to authorise.

---

## Cold-start behaviour

Render free tier spins down after inactivity (15–60 s spin-up). Upstash Redis
keeps the OAuth token alive across restarts so you only authorise once.

If a routine calls this MCP after a long idle period, start with a lightweight
call (`list_labels`) to warm up the container before the main tool call.

---

## Local development

```bash
export ICLOUD_EMAIL=benedikt@wendlinger.me
export ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
export GITHUB_CLIENT_ID=...
export GITHUB_CLIENT_SECRET=...
export SERVER_URL=http://localhost:8000
export UPSTASH_REDIS_REST_URL=...
export UPSTASH_REDIS_REST_TOKEN=...

pip install -e .
python -m icloud_mail_mcp
```

---

## IMAP details

| Protocol | Host | Port | Security |
|---|---|---|---|
| IMAP | `imap.mail.me.com` | 993 | TLS (SSL) |
| SMTP | `smtp.mail.me.com` | 587 | STARTTLS |

iCloud drops idle IMAP connections after ~30 minutes. The client handles
`imaplib.abort` automatically with a single reconnect attempt.

---

## Project structure

```
icloud-mail-mcp/
├── src/
│   └── icloud_mail_mcp/
│       ├── __init__.py           # _build_app(), main()
│       ├── __main__.py           # python -m icloud_mail_mcp
│       ├── github_oauth_provider.py
│       ├── imap_client.py
│       └── mail_tools.py
├── pyproject.toml
├── Dockerfile
└── render.yaml
```
