# ShadowPI – CAS-aware proactive moderator

ShadowPI is a Telegram moderation bot built on `python-telegram-bot` that combines
the CAS (Combot Anti-Spam) API with lightweight behaviour scoring so you can react
instantly to risky joins, floods, or suspicious link drops across your chats.

## Highlights

- **Per-user CAS checks**: Every new member is verified against `https://api.cas.chat/check`.
  Confirmed CAS bans trigger an immediate ban plus a mod-log entry.
- **Bulk CAS export sweeps**: A background job keeps the `export.csv` list mirrored in a
  local SQLite watchlist so later messages from those IDs skip straight to high-risk
  handling even if they joined before the last sync.
- **Profile + scoring**: Per-user metadata (first/last seen, counters, local trust) is
  tracked in `../shadowpi_data/shadowpi.sqlite3` and evaluated on every message for floods,
  repeated copy/paste, premature link drops, forwards-only behaviour, and blacklist hits.
- **Tiered responses**: Thresholds escalate from warn → mute → ban. Actions are logged to
  an optional moderator channel.
- **Manual overrides**: `/allow`, `/banlocal`, and `/override_clear` let admins rescue
  false positives or permanently nuke chronic offenders regardless of CAS status.

## Setup

```
python -m venv bots-env
source bots-env/bin/activate
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="1234567890:ABC..."
# Optional tuning:
export SHADOWPI_DATA_DIR="/var/lib/shadowpi"
export SHADOWPI_MOD_LOG_CHAT="-1001234567890"
export SHADOWPI_KEYWORDS="pump,moon,xxx"
```

Then run the bot:

```
python bot.py
```

## Commands

- `/start` – quick capability summary.
- `/activate` – unlock the bot (default pin `80085`, entered privately).
- `/lock` – relock the bot so automation halts until the pin is re-entered.
- `/import_roster` – DM-based flow to import newline-separated user IDs for sweeps.
- `/stats` – totals for users/messages/warnings/deletes and watchlist size.
- `/cascheck <user_id>` – manual lookup against CAS (admins only).
- `/allow <user_id> [note]` – whitelist a user so CAS hits are ignored.
- `/banlocal <user_id> [note]` – force-ban override regardless of CAS.
- `/override_clear <user_id>` – remove previous override entry.
- `/patrol` – turn proactive enforcement back on.
- `/standdown` – pause automatic enforcement so you can moderate manually.
- `/suspect` – reply to a message to force a manual scan + action even during standdown.
- `/sweep [report|clean] [limit]` – iterate group members, flag deleted accounts, CAS hits,
  and high-risk moles. `clean` automatically kicks deleted accounts and shadowbans
  high-risk profiles. Optional `limit` bounds the number scanned.
- `/shadowban <user_id>` – silently nuke a user so future messages delete instantly.
- `/shadowlift <user_id>` – restore someone from the shadowban list.

Only chat admins can trigger management commands. The bot also obeys a
`SHADOWPI_MOD_LOG_CHAT` ID for central logging; otherwise, it posts enforcement
notes back into the source chat and the process log.

### Activation pin

ShadowPI stays locked after startup until an admin runs `/activate` in the target
group. The bot DMs that admin asking for the pin (default `80085`, override via
`SHADOWPI_ACTIVATION_PIN`) so the code never appears in chat history. When
locked, all commands except `/start` + `/activate` are disabled and no automated
moderation takes place. Use `/lock` to return to the locked state after an
incident.

### Patrol vs standdown

ShadowPI stores a persistent patrol flag (`/patrol` vs `/standdown`). When patrol is
disabled, it still records telemetry but will not auto-welcome, auto-ban, or score
messages until you bring it back online. During standdown, use `/suspect` on a reply
to a suspicious user/message to run the same scoring pipeline and, if necessary,
delete/mute/ban the offender on demand.

### Member sweep / mole detection

`/sweep` walks ShadowPI's stored member records for the current group (seed via
`/import_roster` if you have a list) and computes a
"mole" risk score using:

- Deleted-account shells (auto-kick in clean mode)
- CAS/export hits
- Silent watchers that linger for 7+ days without contributing
- Accounts that forward or drop links immediately after joining
- Profiles with many identity changes (username / display name churn)
- Ghost accounts (no username/last name after 30+ days)
- Cross-group incidents (warnings or deletions recorded anywhere ShadowPI runs)

Scores >=60 are highlighted; >=80 default to shadowban when `clean` is used. The sweep
report summarizes totals plus the top flagged members from ShadowPI's database so
admins can audit or manually remove them. (If someone left before the sweep, their
record may still appear.) Shadowbans persist in the SQLite `users.shadowbanned`
column and take effect instantly on the next message.

### Roster import

Run `/import_roster` in the group to let an admin send a DM containing newline-
separated entries such as `123456789 @handle First Last`. Each line **must** contain
the numeric Telegram user ID; optional usernames and names improve the risk report.
Imported entries populate the SQLite DB so `/sweep` can rate long-time members
immediately, even if they haven't spoken since ShadowPI was deployed.

## Behaviour rules

ShadowPI computes a risk score on each message:

- Flooding (>=5 msgs / 10s) – +20
- Repeating identical text quickly – +15
- Forward-only accounts – +10
- Link inside probation window – +20 (or +5 later)
- Blacklist keyword/domain hit – +30
- CAS export match – score jumps to at least the mute threshold
- CAS /check positive – score jumps over the ban threshold

Thresholds default to `warn=30`, `mute=60`, `ban=100`. Adjust via
`SHADOWPI_WARN_THRESHOLD`, `SHADOWPI_MUTE_THRESHOLD`, `SHADOWPI_BAN_THRESHOLD`.

## Data retention

- SQLite DB at `../shadowpi_data/shadowpi.sqlite3`
- Tables: `users`, `overrides`, `cas_watchlist`, `bot_state`
- Stores counters and timestamps only (no message content), aligning with the
  "metadata not content" guideline from the project brief.

## CAS attribution

Commercial/paid usage of CAS requires attribution. ShadowPI shows
`Powered by CAS (cas.chat)` in `/start` by default, and you can change this via
`SHADOWPI_ATTRIBUTION` while keeping the required credit.
