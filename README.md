# Virtus

**Team goals & performance reviews** for FRC teams 4143 (MARS/WARS) and 4423 (MARS'
Minions). The team's leadership program is built around student ownership and
accountability — Virtus is where that gets written down and followed up on.

- **Team goals** — the season's objectives, each owned by a subteam (or the whole team),
  with a target date and a status (not started / on track / at risk / done). Every member
  sees the board; leadership edits it.
- **Student goals** — each student keeps their own development goals, tracked the same
  way. Deliberately **independent** of the team goals: no roll-up, no forced alignment,
  so "learn to run the CNC unsupervised" is as valid a goal as anything on the season board.
- **Review cycles** — an admin opens a named cycle (e.g. "2026 Build Season Midpoint")
  with a close date and a roster of every active student, and assigns each one a reviewer.
- **Self-review + reviewer review** — the student rates themselves and their assigned
  reviewer rates them, on the **same** form: an admin-editable set of competencies scored
  1–4 with comments, plus what's going well and where to grow.
- **Side by side** — once both are submitted, the student sees the two next to each other
  with the gap on each competency. A student who rates themselves two points below their
  reviewer is exactly the conversation worth having.
- **Completion dashboard** — who's done, who's behind, who has no reviewer yet, plus a
  one-click "remind everyone" and an automatic nudge as the close date approaches.

**A reviewer needs no admin access.** Subteam leads on this team are students, so the
right to write a review comes from being assigned it, not from a Legion group. A lead
writes their reviews at `/me/reviews` and still gets a 403 at `/admin`.

Sibling to **[Tempus](https://github.com/FRC-Team-4143/tempus)** (attendance),
**[Munus](https://github.com/FRC-Team-4143/munus)** (volunteer hours),
**[Merces](https://github.com/FRC-Team-4143/merces)** (student rewards), and
**[Legion](https://github.com/FRC-Team-4143/legion)** (shared roster + SSO). Same stack;
separate app. FastAPI + async SQLAlchemy + Jinja2 + SQLite, port **8006**.

## Quick start (local)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # fill in Slack + Legion values (see below)
uvicorn app.main:app --reload --port 8006
```

- Members: <http://localhost:8006/> (team goals), `/me` (own goals + reviews),
  `/me/reviews` (reviews you owe)
- Admin: <http://localhost:8006/admin>

The SQLite DB is created on first boot, seeded with a starting competency list
(Attendance & Reliability, Technical Growth, Teamwork & Communication, Initiative,
Leadership) — edit it at `/admin/competencies`. Everything else starts empty; the roster
arrives from Legion via `/admin/roster` → **Sync now**.

### Signing in locally

There's no local Legion to mint an `mw_sso` cookie. The gitignored `devlogin.py` helper
stands in for one:

```bash
uvicorn devlogin:app --port 8007
# then open, e.g.:
#   http://localhost:8007/login?code=<member_code>&groups=virtus-admin
#   http://localhost:8007/login?code=<member_code>            (a plain student)
```

It signs a cookie with the app's own `SSO_SECRET` and redirects in. It touches no app code.

## Configuration

See `.env.example`. The values that matter:

| Var | What it's for |
|-----|---------------|
| `SSO_SECRET` | Must be **identical** to Legion's. Verifies the shared `mw_sso` cookie. |
| `LEGION_BASE_URL` / `LEGION_API_KEY` | The read-only roster + subteam API and the one-tap sign-in challenge. The key must match Legion's `VIRTUS_API_KEY`. |
| `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET` | The shared team Slack app. Scopes: `chat:write`, `im:write`. |
| `SLACK_ANNOUNCE_CHANNEL` | Where "a cycle just opened" is posted. Blank = off. |
| `CURRENT_SEASON` | The season new goals and cycles default to. |
| `REVIEW_REMINDER_DAYS` | Start DMing stragglers this many days before a cycle closes. `0` = off. |

There is no admin password. Access comes from Legion groups:

- **`virtus-admin`** — everything.
- **`virtus-manager`** — team goals, review cycles, the completion dashboard, and student
  profiles. Not competencies (editing them changes how the whole team gets assessed),
  roster, settings, backup, audit, or reopening a submitted review.
- **no group** — the portal, plus any review you've been assigned.

## Testing

```bash
pytest
```

100 tests. In-memory SQLite, real (never mocked) database, no outbound Slack.

## Deployment

Deployed alongside the siblings from the `apps-infra` repo (Docker Compose + Nginx Proxy
Manager) on container port **8006**, public URL `virtus.marswars.org`.

**Not yet live.** To ship it:

1. `git init` here and push to `FRC-Team-4143/virtus`; add the repo to the scope of the
   `MARSWARS_APPS_DEPLOY_*` org secrets.
2. Merge the prepared `wip/virtus-wiring` branch in **legion** (groups, API key,
   launcher tile, health check) onto `main` and push — that triggers the production deploy.
3. Merge the prepared `wip/virtus-wiring` branch in **apps-infra** (compose service,
   volume, `deploy.sh`, README runbook) and push.
4. Generate the API key pair: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
   → `VIRTUS_API_KEY` in `legion/.env`, the same value as `LEGION_API_KEY` in `virtus/.env`.
5. Add `virtus.marswars.org` to Legion's `SSO_ALLOWED_RETURN_HOSTS`, and optionally set
   `VIRTUS_PUBLIC_URL` for the launcher tile.
6. Add a DNS A record and an Nginx Proxy Manager proxy host `virtus.marswars.org →
   virtus:8006` with a Let's Encrypt cert.
7. Register the `/virtus` slash command in the shared Slack app, pointing at
   `https://virtus.marswars.org/slack/command`.
8. Grant the first admin `virtus-admin` in Legion's `/admin/groups`.

See `apps-infra`'s README (`## Adding Virtus`) for the runbook, and `CLAUDE.md` for the
design rationale behind the code.
