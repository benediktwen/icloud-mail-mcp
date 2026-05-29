# iCloud Mail MCP

Remote MCP server for Apple iCloud Mail via IMAP. Connects Claude to your iCloud
mailbox over the internet — no local server required. All iCloud email addresses
(aliases and custom domains) on your Apple ID are accessible through a single
connection.

## What it does

Exposes 11 mail tools via MCP so Claude can interact with your iCloud Mail directly:

| Tool | Description |
|---|---|
| `search_emails` | Search by query, date range, unread flag. Returns `{id, subject, from, date, snippet}` |
| `get_email` | Fetch full message body by UID |
| `list_labels` | List all IMAP folders / labels |
| `move_email` | Move a message to a different folder |
| `delete_email` | Move a message to Trash (Deleted Messages) |
| `mark_read` | Mark a message as read |
| `mark_unread` | Mark a message as unread |
| `flag_email` | Flag (star) a message |
| `unflag_email` | Remove the flag from a message |
| `create_draft` | Create a new draft email in the Drafts folder (supports file attachments) |
| `create_draft_reply` | Create a pre-filled reply draft for an existing message (supports file attachments) |

## How it works

```
Claude → /authorize → GitHub login (+ 2FA) → /auth/callback
       → username verified → MCP access token issued → MCP connection
```

Access is protected by **GitHub OAuth** — only the GitHub account set in
`GITHUB_ALLOWED_USER` can authenticate. GitHub login with 2FA is required
once every 30 days (tokens are persisted to Redis). No shared secrets are
stored in Claude's config.

> **Cold start note:** If the hosting platform sleeps the container, the first
> request after wake-up takes a few seconds. OAuth tokens are persisted to
> a Redis-compatible store so Claude does **not** need to re-authenticate.
> iCloud drops idle IMAP connections after ~30 minutes — the client handles
> this automatically with a single reconnect attempt.

## Deploy your own

You will need:

- A container hosting platform (e.g. Render, Railway, Fly.io)
- A Redis-compatible key-value store for token persistence (e.g. Upstash, Redis Cloud)
- A GitHub OAuth App for authentication
- An Apple ID with iCloud Mail and an app-specific password

### Step 1 — Apple app-specific password

iCloud requires an app-specific password — your main Apple ID password will not work.

1. Go to [appleid.apple.com](https://appleid.apple.com) → **Sign-In and Security** → **App-Specific Passwords**
2. Click **+** and label it (e.g. `icloud-mail-mcp`)
3. Copy the generated password (format: `xxxx-xxxx-xxxx-xxxx`)

### Step 2 — Redis store

Create a Redis database on your preferred provider. Note the **REST URL** and **auth token**.

### Step 3 — GitHub OAuth App (one-time)

Create a GitHub OAuth App at **Settings → Developer settings → OAuth Apps**:

- **Application name:** anything (e.g. `My MCP Servers`)
- **Homepage URL:** `https://your-service-url`
- **Callback URL:** `https://your-service-url/auth/callback`

Note the **Client ID** and generate a **Client Secret**.

### Step 4 — Deploy

1. Fork this repo
2. Deploy to your container hosting platform (a `render.yaml` is included for Render)
3. Set the environment variables listed below
4. Trigger a deploy

### Step 5 — Configure Claude

In Claude.ai web (connector dialog):
- **URL:** `https://your-service-url/mcp`
- OAuth fields: leave empty — the server advertises its own OAuth metadata

Claude Desktop and mobile sync automatically from the web connector.

## Configuration reference

| Env var | Required | Rotates | Description |
|---|---|---|---|
| `ICLOUD_USERNAME` | ✅ | Never | Your iCloud email address (primary Apple ID email) |
| `ICLOUD_APP_PASSWORD` | ✅ | On reset | Apple app-specific password |
| `GITHUB_CLIENT_ID` | ✅ | Never | GitHub OAuth App client ID |
| `GITHUB_CLIENT_SECRET` | ✅ | Never | GitHub OAuth App client secret |
| `GITHUB_ALLOWED_USER` | ✅ | Never | GitHub username allowed to connect |
| `SERVER_URL` | ✅ | Never | Public base URL of this service |
| `UPSTASH_REDIS_REST_URL` | ✅ | Never | Redis REST endpoint |
| `UPSTASH_REDIS_REST_TOKEN` | ✅ | Never | Redis auth token |
| `TOKEN_STORE_KEY` | — | Never | Redis key namespace (default: `mcp:icloud-mail:token_store`) |

## IMAP details

| Protocol | Host | Port | Security |
|---|---|---|---|
| IMAP | `imap.mail.me.com` | 993 | TLS |
| SMTP | `smtp.mail.me.com` | 587 | STARTTLS |

## Architecture

- **Transport:** Streamable HTTP (MCP 1.x) via FastMCP + uvicorn
- **Auth:** GitHub OAuth 2.0 — server acts as Authorization Server, GitHub as Identity Provider
- **User restriction:** GitHub username verified against `GITHUB_ALLOWED_USER` on every login
- **Token lifetime:** 30-day access + refresh tokens (persisted to Redis)
- **Token persistence:** Redis-compatible store — tokens survive container restarts
- **Mail API:** Apple IMAP at `imap.mail.me.com` using Apple ID + app-specific password
