from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore": tolerate leftover keys in a deployed .env instead of failing to boot.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    slack_bot_token: str = ""
    slack_signing_secret: str = ""

    # Where "a review cycle just opened" / "reviews still outstanding" announcements go
    # (channel ID, e.g. C0ABCDE123). Blank = channel posts are skipped; per-person
    # reminders are DMs and don't need this. The bot must be a member of the channel.
    slack_announce_channel: str = ""

    # Legion SSO — /admin and the portal are gated by the shared `mw_sso` cookie. Virtus
    # only *verifies* the cookie (Legion mints it); `sso_secret` must equal Legion's
    # SSO_SECRET. There is no local admin password — the first admin is granted
    # `virtus-admin` in Legion's /admin/groups.
    sso_secret: str = ""
    sso_session_ttl: int = 43200  # 12h; must match Legion's cookie max_age
    sso_cookie_domain: str = ""   # e.g. ".marswars.org" so one login spans subdomains

    # Legion roster API + one-tap SSO challenge — the read-only source of truth Virtus
    # mirrors people (and subteams) from, and the server-to-server trigger for one-tap
    # sign-in links.
    legion_base_url: str = ""     # e.g. "https://legion.marswars.org"
    legion_api_key: str = ""      # presented as X-API-Key to Legion's /api/* and /sso/challenge

    database_url: str = "sqlite+aiosqlite:///./virtus.db"

    timezone: str = "America/New_York"

    # Public base URL used when Slack messages link back to Virtus.
    base_url: str = "http://localhost:8006"

    # The season new goals and cycles default to. A plain string (not an int) because
    # teams label seasons inconsistently ("2026", "2025-26") and nothing does arithmetic
    # on it — it's only ever grouped and displayed. Editable at /admin/settings.
    current_season: str = "2026"

    # How many days before a cycle closes the daily reminder job starts DMing people who
    # still owe a review. 0 disables the reminder.
    review_reminder_days: int = 3

    # How many personal (development) goals a student must have on file for a season. The
    # portal nags them until they do, and a self-review can't be submitted while short.
    # 0 disables the requirement entirely.
    required_personal_goals: int = 2

    # Dev / preview sign-in shim. When set, mounts `/dev-login` (see routers/dev_login.py),
    # which mints an `mw_sso` cookie for THIS host — needed only on a preview deploy where
    # Legion's real cookie (scoped to .marswars.org) can't reach. MUST stay unset in
    # production; every /dev-login request has to present this exact value.
    dev_login_secret: str = ""

    # Database backups (SQLite only)
    backup_dir: str = "backups"
    backup_keep: int = 14  # number of snapshots to retain
    backup_time: str = "23:30"  # HH:MM 24h local time for the weekly snapshot
    backup_day: str = "sun"  # day of week for the weekly backup (mon-sun)

    # Global toggle for all automated updates (Slack messages, scheduled jobs)
    updates_enabled: bool = True


settings = Settings()
