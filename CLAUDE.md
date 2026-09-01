# Virtus — Codebase Guide

**Team goals & performance reviews** for FRC teams 4143 (MARS/WARS) and 4423 (MARS'
Minions). Leadership records the season's **team goals**; students keep their own
**development goals**; and inside admin-opened **review cycles** each student writes a
**self-review** while an assigned reviewer writes the **official review**, on the same
form, shown side by side. FastAPI + SQLAlchemy (async) + Jinja2 + SQLite.

Sibling to **Tempus** (attendance), **Munus** (volunteer hours), **Merces** (rewards),
and **Legion** (shared roster + SSO). Intentionally mirrors their stack, dark theme, and
conventions, but is a fully separate app with its own DB, Slack config, and Docker
service (**port 8006**). Nothing is imported across the projects — integration with
Legion is over HTTP only.

## Running

```bash
source venv/bin/activate
uvicorn app.main:app --reload --port 8006
```

Requires a `.env` (see `.env.example`). Key vars: `SLACK_BOT_TOKEN`,
`SLACK_SIGNING_SECRET`, `BASE_URL`, `CURRENT_SEASON`, and the Legion integration —
`SSO_SECRET` (must equal Legion's), `LEGION_BASE_URL`, `LEGION_API_KEY`. There is **no**
admin password; `/admin` is gated by Legion SSO + the `virtus-admin` (full) or
`virtus-manager` (team goals, cycles, dashboard) group. The portal is open to any active
roster member.

No Legion runs locally, so there's nothing to mint an `mw_sso` cookie for development.
The gitignored `devlogin.py` helper mints one signed with Virtus's own `SSO_SECRET` and
redirects into the app — run it on another port (`uvicorn devlogin:app --port 8007`) and
hit `/login?code=<member_code>&groups=virtus-admin`. It touches no app code.

## Testing

```bash
pytest
```

In-memory SQLite with async `pytest-asyncio`. **Do not mock the database** — tests hit a
real (in-memory) DB. `tests/conftest.py` provides a `FakeSlack` recorder (no outbound
Slack), `make_sso_cookie()` / `cookie_for(member)`, `admin_cookie`/`manager_cookie`
fixtures, and `make_member` / `make_cycle` factories.

Two fixtures are worth knowing about. `_isolate_settings_from_dotenv` (copied from
Legion) resets every setting to its class default so a developer's real `.env` can't
change a test's outcome — and because `services/sso.py` and `services/legion_auth.py`
build their signers at *import* time from `settings.sso_secret`, it also calls
`_rebuild_signers()`; without that, every cookie the suite mints would fail verification
against a signer still holding the old key.

## Project Layout

```
app/
  main.py            # FastAPI app, router wiring, lifespan (init_db + scheduler), /health
  config.py          # Settings (pydantic-settings, reads .env)
  database.py        # Engine, session, init_db() + seed_competencies()
  models.py          # ORM models
  utils.py           # Naive-UTC datetime helpers (utc_to_local, now_utc)
  templating.py      # Shared Jinja2 env (filters + auth-aware globals)
  routers/
    portal.py        # /  team-goals board; /me goals; self-review; "reviews I owe"
    admin.py         # /admin — team goals, competencies, cycles, students, roster, ops
    slack.py         # /virtus slash command
  services/
    goals.py         # Team goals + student goals (two independent lists)
    cycles.py        # Cycle lifecycle, roster, reviewer assignment, completion stats
    reviews.py       # Review read/write authorization, save/submit, side-by-side
    notify.py        # Slack announcements + reminders (magic links, never bare URLs)
    sso.py           # Verifies Legion's mw_sso cookie (verify-only) + group helpers
    legion_sync.py   # Pulls roster *and subteams* from Legion into the local mirror
    legion_auth.py   # Magic links (preferred) + the older SSO challenge round trip
    slack_client.py  # AsyncWebClient wrapper (send_dm / post_to_channel / update_message)
    scheduler.py     # APScheduler: hourly Legion sync, daily review reminders, backup
    backup.py        # SQLite snapshot backup + staged restore (VACUUM INTO)
    audit.py         # Append-only mutation log
    app_settings.py  # Persisted runtime settings (legion sync watermark)
```

## Domain model (`app/models.py`)

`Member` — roster mirror keyed on Legion `member_code`, with `kind`, `group_slugs`, and
**`subteam_slug`/`subteam_label`** synced from Legion. `Subteam` — mirror of Legion's
subteam list. `TeamGoal` / `StudentGoal` — the two goal lists. `Competency` — the
admin-editable master list of rated dimensions. `ReviewCycle` → `CycleCompetency`
(the frozen form) and `ReviewAssignment` (one student's slot + who reviews them) →
`Review` (`self` | `reviewer`) → `ReviewRating` (one score + comment per competency).
Plus `AppSetting` and `AuditLog`.

## Key conventions

### Writing a review is authorized by the assignment, not by a Legion group
This is the decision the whole app is shaped around. Subteam leads on this team are
**students**, and they have to be able to review their members — so if the reviewer form
lived behind `virtus-admin`/`virtus-manager` like every other staff-ish surface in the
sibling apps, every lead would need admin access to the roster, the audit log, and the
settings just to write a review.

Instead the reviewer form lives in the **portal** (`/me/reviews`, `routers/portal.py`),
and the right to write comes from being named on the `ReviewAssignment`
(`services/reviews.can_write`). A lead with zero Legion groups gets a 403 at `/admin` and
still writes their assigned reviews. `/admin` keeps only what it should: building cycles,
editing goals and competencies, and the completion dashboard.

Staff are deliberately **not** granted write access by `can_write` either — an admin who
needs to write a review assigns it to themselves first, so the authorship on the record
always matches who was actually responsible for it.

### Opening a cycle freezes its competency list
`services/cycles.open_cycle()` snapshots every active `Competency` into
`CycleCompetency` rows. Reviews then rate the *snapshot*, never the master list — the
same reasoning as Merces's `Redemption.item_name`/`cost` snapshot. Two things fall out of
it: renaming or archiving a competency can't rewrite the questions a finished cycle was
answered against, and self and reviewer are *guaranteed* to have answered the identical
form, which is what makes the side-by-side comparison meaningful at all.

Reopening a closed cycle keeps the original snapshot rather than re-snapshotting — the
reviews already submitted were written against it.

`open_cycle` ends with `await db.refresh(cycle, ["competencies"])`. The snapshot rows are
inserted by FK rather than through the relationship, so the loaded collection would
otherwise be stale — and on a persistent object, touching a stale collection fires a lazy
load, which raises `MissingGreenlet` under async SQLAlchemy.

### Goals are two separate lists
A `StudentGoal` has **no** foreign key to a `TeamGoal` and never rolls up into one. That
was an explicit product call: forcing every personal goal to ladder up to a season
objective would push out the "learn to run the CNC" / "speak up in design reviews" goals
that are the point of a development plan. The team board and a student's goal list are
just two views that happen to share a `GoalStatus` vocabulary and a `season` string.
`tests/test_goals.py` asserts the absent FK, so it can't quietly come back.

### Students must keep N personal goals per season
`settings.required_personal_goals` (default **2**, `0` = off). A student sets their own at
`/me` (the "My Goals" card — `POST /me/goals`, plus edit/status/delete; staff can also
add one from `/admin/students/{code}` while coaching). `services/goals.personal_goal_
shortfall(member_id, season=…)` is the single source of truth for "how many more do they
owe". It drives two things: a warning banner on `/me` (with the add-goal form sprung
open), and a **hard gate on submitting a self-review** — `routers/portal._save_review`
refuses `submit` for a `self` review while the student is short, keeping their typed
answers as a draft and re-rendering the form with the reason. The reviewer's form shows
the subject's `n / required` count too. The count is per `season`; the review gate checks
the *cycle's* season, the `/me` banner checks `current_season()`.

### Team goals are categorised and team-scoped
Every `TeamGoal` carries two fixed classifiers, both required enums (no free text, no
roster link):
- **`category`** (`GoalCategory`): `Robot Performance` → `Award` → `Learning and Culture`,
  in that order. This is what the board buckets into cards and what
  `services/goals.group_team_goals_by_category()` iterates — every category shows on the
  board once the season has any goal at all, an empty one included, so it reads as a
  scorecard. `list_team_goals()` sorts by this order (a stable sort over a
  `sort_order`/`title` query), not in SQL.
- **`team`** (`GoalTeam`): `4143` / `4423` / `organization` (the whole program, not a
  third team). The board and the admin list both take a `?team=` filter; blank = all.
  Stored **by value** (`"4143"`), so its `SAEnum` needs `values_callable` — the member
  names can't start with a digit.

This replaced an earlier "group team goals by their owning Legion subteam" model. Team
goals no longer touch `subteams` at all; the `Subteam` mirror now only powers cycle
rostering ("assign a whole subteam to reviewer Y") and roster display.
`_migrate_team_goal_category` in `database.py` is the one-shot migration (adds both
columns with constant defaults, drops `team_goals.subteam_slug`).

### Draft privacy vs. submitted visibility
`services/reviews.can_read()` is the one place this lives. A **draft** is private to its
author — including from the student it's about, so nobody reads a half-written assessment
of themselves. Once **submitted** it opens to the subject student and to all staff.

### Private notes — visible to the reviewer and staff, never to the student
`Review.private_notes` is a reviewer-only free-text field (rendered only on the
`reviewer`-kind form, `routers/portal._save_review` refuses to set it on a self-review).
Its gate is `services/reviews.can_read_private_notes()`, deliberately **stricter** than
`can_read`: the subject of the review never sees the notes — not after submission, and
not even if that subject holds a `virtus-admin`/`virtus-manager` group (the
subject-exclusion check runs first and wins). Author sees them; staff above the reviewer
see them. They're frozen on submit like the rest of the review — a reviewer who needs to
change one after submitting goes through an admin `/unsubmit`. Surfaced to staff on
`admin/student.html` (suppressed there too when a staff member is viewing their *own*
profile, via `show_private_notes`); never rendered on the student-facing
`portal/review_result.html` at all.

Submission is one-way for its author: `save()` refuses to touch a submitted review rather
than silently ignoring the write, so a stale browser tab can't overwrite something
someone already stood behind. Reopening one is `unsubmit()`, admin-only and audited
(`/admin/cycles/{id}/reviews/{id}/unsubmit`, excluded from `_manager_allowed`).

### A refused submit keeps the draft
`services/reviews.save()` writes the answers *before* it checks that every competency is
rated, so when the submit check fails everything typed is already staged. The router
therefore **commits and re-renders with the error** rather than rolling back — a rollback
would throw away the person's work and hand them a blank form to redo. (It would also
expire the session mid-request and blow up the error re-render on a lazy load.)

### Season is a string
`TeamGoal.season` / `ReviewCycle.season` are `String`, not `Integer`: teams label seasons
inconsistently ("2026", "2025-26") and nothing does arithmetic on it — it's only ever
grouped and displayed. `settings.current_season` is the default for new records.

### Subteams are mirrored, and never deleted
`services/legion_sync._upsert_subteams()` mirrors `/api/subteams`. A subteam that
disappears from Legion's list is **deactivated, not deleted** — a closed cycle's grouping
still needs its label to render as words rather than a bare slug. `is_active` is what
hides it from the pickers. `Member.subteam_slug` and `ReviewAssignment.subteam_slug` are
deliberately **not** foreign keys for the same reason: a stale slug should degrade to
"show the slug", not fail an insert or orphan a row. (Team goals used to be grouped by
subteam too; they're categorised now — see "Team goals are categorised and team-scoped".)

### Slack links are magic links
Everything Virtus sends into Slack uses `legion_auth.make_link_url` (Munus's newer path),
never a bare URL: Slack's in-app browser drops cookies between opens, so a plain link
would face the recipient with a fresh Approve/Deny push every single time. A magic link
is a bearer credential, so it only ever goes into a **DM** or an ephemeral slash-command
reply — the announce-channel post gets a plain URL.

### Service/router transaction boundary
Domain services `flush()`, never `commit()` — so a caller can read back what was just
written — and the *router* commits alongside its own `audit.record()` call. Same
convention as every sibling app.

### Datetimes
All DB datetimes are **naive UTC** (`app/utils.py`): `utc_to_local()` for display,
`now_utc()` for "now". Cycle windows are day-granular in the UI; `_parse_datetime` in
`routers/admin.py` reads a `<input type=date>` as **end of day**, because "closes on the
14th" has to mean the end of the 14th or a cycle open all day is already past its close
the moment it begins.

### Legion integration (source of truth for the roster)
Legion owns members, subteams, and groups; Virtus is a **read-only consumer** — data
flows Legion → Virtus only. `services/sso.py` verifies the `mw_sso` cookie locally with
the shared `SSO_SECRET` (no callback); on a miss, redirect to
`{LEGION_BASE_URL}/sso/authorize?app=virtus`. **Never add roster CRUD or write-back.**

`make_authorize_url` makes an explicit relative `return_to` **absolute** before handing
it to Legion — Legion's `/sso/complete` redirects to it as-is, and a bare path would
resolve against Legion's own host. (Merces's copy still has this bug; Munus's doesn't.)

### Database migrations
No Alembic. New tables are picked up automatically by `create_all()`; an additive column
on an *existing* table needs an inspect-guarded `ALTER`, called from `init_db()` after
`create_all()` — see the sibling apps for the pattern. None are needed yet.

### Seed data
`seed_competencies()` inserts `DEFAULT_COMPETENCIES` and is the only seed data in the
app. It runs **only when the table is completely empty**, deliberately all-or-nothing: an
admin who deletes a competency they don't want must not have it reappear on the next boot.

## UI conventions
Single dark theme shared with the siblings (`#0a0a0a` bg, `#111111` panels, accent red
`#cc2200`, borders `#2a1a1a`). Admin pages extend `admin/base.html` (Bootstrap 5,
sidebar); the portal extends `portal/base.html` (navbar). `_macros.html` holds the shared
status chip / score pill / review-state renderers so a colour means the same thing on
both sides of the app.

The review rating scale is **1–5** (`SCORE_LABELS` in `models.py`: Needs support →
Developing → Solid → Strong → Exceptional), rendered as a **red→blue rainbow** worst-to-
best — 1 red, 2 orange, 3 yellow, 4 green, 5 blue. The colours live in `.score-N`
(read-only pill, via the `_macros.score` macro) and `.score-choice-N` (the radio buttons
on the review form) in *both* base templates, keyed on the score number. This replaced
the original 4-point "no neutral middle" scale — adding the 5th point brings back a
centre value, which was the deliberate cost of getting five distinct rainbow steps.

A `<form>` can't be a child of `<tr>`, so the editable tables (`admin/competencies.html`,
`admin/cycle_detail.html`) put each row's form after the table and point the inputs at it
with the HTML `form="..."` attribute.

## Scheduled jobs (`scheduler.py`)

| Job | Trigger |
|-----|---------|
| Legion roster + subteam sync | hourly, on the hour |
| Review reminders (unsubmitted, inside `REVIEW_REMINDER_DAYS` of `closes_at`) | daily at 10:00 local |
| Database backup | `BACKUP_DAY` at `BACKUP_TIME` (SQLite snapshot, rotates to `BACKUP_KEEP`) |

The reminder job runs off the same `cycles.outstanding_reviews()` the admin "Remind
everyone" button uses, so the automated nudge and the manual one can never disagree about
who is behind.

## Deployment
Deployed alongside the siblings from the `apps-infra` repo (Docker Compose + Nginx Proxy
Manager) on container port **8006**, public URL `virtus.marswars.org`. **Not yet wired
up** — this repo needs `git init` + push to `FRC-Team-4143`, and the Legion/apps-infra
changes are prepared on `wip/virtus-wiring` branches in each repo rather than on `main`
(per the house rule that an app's wiring only lands on `main` when it's actually going
live). See `README.md` for the full go-live checklist.
