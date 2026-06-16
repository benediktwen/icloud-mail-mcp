# iCloud Mail MCP

Remote MCP server for Apple iCloud Mail via IMAP. Gives AI assistants access to your iCloud mailbox over the internet — no local server or app required. All email addresses on your Apple ID — aliases and custom domains — are accessible through a single connection.

## What it does

Exposes 11 mail tools so AI assistants can interact with your iCloud Mail directly:

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
AI assistant → /authorize → GitHub login (+ 2FA) → /auth/callback
             → username verified → MCP access token issued → MCP connection
```

Access is protected by **GitHub OAuth** — only the GitHub account set in
`GITHUB_ALLOWED_USER` can authenticate. GitHub login with 2FA is required
once every 30 days; tokens are persisted to Redis. No credentials are stored
in the AI assistant's configuration.

1. The AI assistant detects the MCP server requires OAuth
2. A browser window opens — you log in to GitHub with 2FA
3. The server verifies your GitHub account matches `GITHUB_ALLOWED_USER_ID`
4. The AI assistant receives a 30-day access token and a 30-day refresh token

> **Cold start note:** If the hosting platform sleeps the container, the first
> request after wake-up takes a few seconds. OAuth tokens are persisted to
> a Redis-compatible store so the AI assistant does **not** need to re-authenticate.
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

### Step 5 — Connect to your AI assistant

In your MCP-compatible AI assistant, add this server as a remote MCP connection:

- **URL:** `https://your-service-url/mcp`
- Authentication: leave empty — the server handles OAuth automatically

**For Claude:** paste the URL into the connector dialog at [claude.ai](https://claude.ai). Claude Desktop and mobile sync automatically from the web connector.

## Configuration reference

| Env var | Required | Rotates | Description |
|---|---|---|---|
| `ICLOUD_USERNAME` | ✅ | Never | Apple ID email |
| `ICLOUD_APP_PASSWORD` | ✅ | On reset | Apple app-specific password |
| `GITHUB_CLIENT_ID` | ✅ | Never | GitHub OAuth App client ID |
| `GITHUB_CLIENT_SECRET` | ✅ | Never | GitHub OAuth App client secret |
| `GITHUB_ALLOWED_USER_ID` | ✅ | Never | Immutable numeric GitHub user ID allowed to connect (preferred) |
| `GITHUB_ALLOWED_USER` | — | Never | GitHub username — legacy fallback, used only if `GITHUB_ALLOWED_USER_ID` is unset |
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
- **User restriction:** GitHub user ID verified against `GITHUB_ALLOWED_USER_ID` on every login (immutable; `GITHUB_ALLOWED_USER` is a legacy username fallback)
- **Token lifetime:** 30-day access token, 30-day refresh token (rotated on each refresh)
- **Token persistence:** Redis-compatible store — tokens survive container restarts
- **Mail API:** Apple IMAP at `imap.mail.me.com` using Apple ID + app-specific password

## Contributing

This code was built with AI assistance ([Claude Code](https://claude.ai/code)) — vibe-coded with the best intentions. Security has been a priority throughout, but the code has not been independently audited. Use it at your own risk. If you spot a bug, a vulnerability, or an opportunity to improve anything, issues and pull requests are very welcome.

## Credits

Built with [Claude Code](https://claude.ai/code).
