# AGENTS.md — Zulip Fork (zulip-edenu)

⛔ **STOP**: Do NOT run `git commit` or `git push` unless the user explicitly says to.
Prepare changes, show status/diff, and WAIT. Only commit and push on explicit instruction.

**Note on `.claude/CLAUDE.md`:** that file is **upstream Zulip's** contribution guide
(testing, code style, commit discipline). It's useful reference, but this fork **overrides**
its commit strategy — we squash all custom work into a **single commit** (see
[Branch & Commit Strategy](#branch--commit-strategy)), not upstream's many-minimal-commits approach. Follow CLAUDE.md's **understand → propose → implement → verify** workflow, but: (1) "understand" means interview me to surface the real goal, not just the stated task; (2) "propose" includes stating acceptance criteria up front before coding.

## Project Overview

Custom Zulip fork with Portal Edenu production customizations.

**Tech Stack:**

- Python 3.12 + Django + Tornado
- Frontend: TypeScript + jQuery + Handlebars
- CI: GitHub Actions (`przwr/zulip-edenu`)
- Deployment: VPS via `upgrade-zulip-from-git` script

**Git:**

- Upstream remote: `https://github.com/zulip/zulip` (branch `12.x`)
- Custom remote: `https://github.com/przwr/zulip-edenu.git` (branch `portal-12.x`)
- **NEVER commit or push without explicit user instruction** — prepare changes, show git status/diff, and wait

**Environment Awareness:**

- **Development (YOU)**: Local machine, edit code, run lint checks
- **VPS Production (USER)**: Runs deployment commands provided by you
- Never assume you're on VPS

## Knowledge Graph (graphify)

**If `graphify-out/graph.json` ever exists in this repo**, treat it as the
primary tool for understanding the codebase — query it before `grep`/`rg` for
architecture, call-flow, and symbol-location questions. It is **CWD-relative
with no `--graph` flag**, so run from this repo root:

```bash
[ -f graphify-out/graph.json ] && $(cat graphify-out/.graphify_python) -m graphify query "how does SAML profile field sync work?"
```

Currently there is **no graph in this repo** (no `graphify-out/`). If you need
one, invoke the `/graphify` skill — but note this is a large Django/TS codebase
and a full build is slow; only build it if you'll do sustained work here.

## Branch & Commit Strategy

- All custom changes in **1 squashed commit** on top of `upstream/12.x`
- Easy to rebase onto future upstream versions
- Amend the single commit with each fix: `git commit --amend --no-edit`
- Force push after amend: `git push --force-with-lease origin portal-12.x`

**Customization Rules:**

- Mark ALL custom code with `# PORTAL EDENU:` comment prefix
- Customizations are **additive** — extend, never modify upstream code in place
- Production-only code MUST be gated on `settings.PORTAL_EDENU` (defaults `False`)
- Test files CAN be modified to add custom tests (with `# PORTAL EDENU:` prefix)

## Production-Only Code Gating

**Backend:**

- `settings.PORTAL_EDENU = False` in `zproject/default_settings.py`
- Set `True` in production `/etc/zulip/settings.py`
- Gate ALL production-only restrictions: `if settings.PORTAL_EDENU:`
- DB-touching `@receiver`/`post_save`/middleware MUST be gated: the test suite fires these signals (e.g., on every `self.login()`), and any query they run leaks into `assert_database_query_count` blocks → uniform `+N` failures across unrelated tests. (The `refresh_avatar_on_login` daemon-thread signal bit us this way.)

**Frontend:**

- Pass `server_portal_edenu` through the state pipeline:
  1. `zerver/lib/events.py` → `state["server_portal_edenu"] = settings.PORTAL_EDENU`
  2. `web/src/state_data.ts` → zod schema `server_portal_edenu: z.boolean()`
  3. `web/src/settings_account.ts` → gate `.hide()` calls on `realm.server_portal_edenu`

## CI Rules

**Before pushing, verify locally:**

```bash
ruff check --quiet
ruff format --quiet --check
```

Ruff version is pinned in `uv.lock` — must match CI version.

**Pre-push hook:** `.git/hooks/pre-push` (local, not committed) runs `ruff check` + `ruff format --check` on changed `.py` files automatically. Bypass: `git push --no-verify`. Uses host ruff (may drift ~1 patch from CI's pinned version) — CI is authoritative.

**Backend lint rules (only caught by `./tools/lint --groups=backend`, NOT ruff) — verify in Vagrant before pushing:**

- `transaction.atomic()` MUST pass `durable=True` or `savepoint=False` explicitly (custom_check.py rule)
- Use `assert_length(x, n)`, never `assertEqual(len(x), n)` — and `assert_length` has **no `msg=` kwarg**
- gitlint: commit title regex `^(.+:\ )?[A-Z].+\.$` — must end with `.` and have a capital after `prefix: `
- mypy: strict + django-stubs plugin (see [Mypy in Zulip](#session-learnings) gotchas below)

**`uv.lock` gotcha:** `vagrant up --provision` / `uv run` rewrites `uv.lock` (thousands of lines). Always `git restore uv.lock` before committing — CI pins ruff's version from it.

**Running the backend test suite locally (Vagrant/Docker):**

Start (one-time): `vagrant up --provision` (repo root; Docker provider). Stop/remove: `vagrant halt` / `vagrant destroy`. Repo is bind-mounted inside the container; services (Postgres/Redis/RabbitMQ/Memcached) run inside the container, not the host.

Check the VM is up: `vagrant status` (should show `running`). Repo is bind-mounted at `/home/prz/projects/PORTAL/zulip-edenu` inside the container.

```bash
# Run a single test (~1s) — the fast edit/refresh loop:
vagrant ssh -c 'cd /home/prz/projects/PORTAL/zulip-edenu && source .venv/bin/activate && ./tools/test-backend zerver.tests.test_presence.UserPresenceTests.test_query_counts'

# By keyword / file / module:
#   ./tools/test-backend test_query_counts
#   ./tools/test-backend zerver.tests.test_presence
# Full suite ~1 min. Coverage (matches CI's coverage gate): add --coverage.

# FULL backend lint (mypy + custom_check + ruff) — the real CI gate:
vagrant ssh -c 'cd /home/prz/projects/PORTAL/zulip-edenu && ./tools/lint --groups=backend'
# ~1-2 min first run (mypy startup). Catches everything ruff can't:
# mypy, the transaction.atomic(durable=...) rule, assert_length, etc.
```

**IMPORTANT — host mypy is broken:** `./.venv/bin/mypy` and `python -m mypy` both fail with `No module named 'mypy'` on the host (shim present, package not importable). Host `.venv` only has `ruff`. So **mypy and the custom checks (`durable`, `assert_length`, gitlint) can ONLY run in Vagrant** — never claim "all gates green" from ruff alone. Run `./tools/lint --groups=backend` in Vagrant before pushing.

Lint a single file on the host — fast, but ruff-only (no mypy/custom checks):

```bash
./tools/lint zerver/signals.py
```

**Cross-repo edits (portal-edenu → here):** This repo's `pyproject.toml` enforces ruff **line-length 100**; the sibling `portal-edenu` repo uses **120**. When editing files in this repo from inside portal-edenu, portal-edenu's pre-commit hooks do **not** run on this repo's files (different repo, not staged there) — so they give zero protection and will let a >100-char line through to this repo's CI. Before committing any change made cross-repo, `cd ../zulip-edenu` and run `ruff format <path> && ruff check <path>` — plain `ruff` picks up this repo's `pyproject.toml` as cwd. Never trust portal-edenu's pre-commit for files in this repo.

**OpenAPI Schema (`zerver/openapi/zulip.yaml`):**

- Has `additionalProperties: false` on `/register` response schema
- Every new field added to register response MUST be added to:
  1. `zerver/openapi/zulip.yaml` — in the schema section (find nearby field, insert alphabetically)
  2. `zerver/tests/test_home.py` — `expected_page_params_keys` sorted list
- Missing either location = CI failure across ALL register endpoint tests

**Django Management Commands:**

- Use `@override` decorator on `add_arguments` and `handle`
- Type `parser` as `ArgumentParser` (not `object`)
- Type `**options` as `Any` (not `object`)
- Import: `from argparse import ArgumentParser` and `from typing_extensions import override`

**Django Loggers:**

- Logger names use Django module path (e.g., `"zulip.registration"`)
- NOT file path (e.g., `"zerver.views.registration"`)
- Always check the actual `logging.getLogger()` call in source before writing `assertLogs()`

**Test Isolation:**

- Test emails and names must be unique across test methods in the same class
- Avoid reusing emails like `"newuser@zulip.com"` in multiple tests — use descriptive unique ones like `"syncfail@zulip.com"`

## Key Files for Custom Features

| File                                    | Purpose                                            |
| --------------------------------------- | -------------------------------------------------- |
| `zproject/default_settings.py`          | Add `PORTAL_EDENU = False`                         |
| `zproject/backends.py`                  | SAML auth pipeline, custom profile field sync      |
| `zerver/views/registration.py`          | Post-creation hooks (logger: `zulip.registration`) |
| `zerver/views/user_settings.py`         | Settings restrictions (API key, privacy)           |
| `zerver/views/users.py`                 | Self-deactivation blocking                         |
| `zerver/views/custom_profile_fields.py` | Hide internal fields from non-owners               |
| `zerver/lib/events.py`                  | Add fields to event state                          |
| `zerver/tests/test_home.py`             | Register `expected_page_params_keys`               |
| `zerver/openapi/zulip.yaml`             | OpenAPI schema                                     |
| `web/src/state_data.ts`                 | Zod schema for new fields                          |
| `web/src/settings_account.ts`           | Hide UI elements conditionally                     |

## Production Settings

In `/etc/zulip/settings.py` on VPS:

```python
NAME_CHANGES_DISABLED = True
AVATAR_CHANGES_DISABLED = True
ENABLE_GRAVATAR = False
PORTAL_EDENU = True
```

Settings that must NOT be in `default_settings.py` (only in production config):

- `PORTAL_EDENU=True`
- `ENABLE_GRAVATAR=False`
- `AVATAR_CHANGES_DISABLED=True`
- `NAME_CHANGES_DISABLED=True`

## Deployment

```bash
sudo /home/zulip/deployments/current/scripts/upgrade-zulip-from-git \
  --remote-url https://github.com/przwr/zulip-edenu.git \
  portal-12.x
su zulip -c '/home/zulip/deployments/current/scripts/restart-server'
```

Deployment doc: `portal-edenu/zulip/zulip-custom-deploy.md`

## SAML Custom Profile Fields Pipeline

1. `zproject/backends.py`: `SocialAuthSyncNewUserInfo` dataclass carries `custom_profile_field_name_to_value`
2. `social_auth_sync_user_attributes()` includes it when `user_profile is None` (new user)
3. `ExternalAuthDataDict` passes it through
4. `zerver/views/auth.py`: `maybe_send_to_registration()` stores custom fields in session as JSON
5. `zerver/views/registration.py`: After `do_create_user()`, reads from session, calls `sync_user_profile_custom_fields(user_profile, custom_field_name_to_value, logger)` — **logger argument is required**

**SOCIAL_AUTH_SYNC_ATTRS_DICT key format:**

Keys after `custom__` must be lowercased underscore-joined field names:

- `custom__profilowe` (not `custom__Profilowe`)
- `custom__data_urodzenia` (not `custom__Data Urodzenia`)
- Because `validate_custom_profile_field_data_for_sync()` normalizes via `"_".join(name.lower().split(" "))`

## Session Learnings

**Mypy in Zulip:**

- `QuerySet` to `list` reassignment needs explicit type annotation
- Missing `return None` at end of function breaks mypy `[return]` check
- `user_profile is not None` may be redundant if mypy already narrowed the type in that branch
- Management commands: `@override`, `ArgumentParser` type, `Any` for options — all required
- **django-stubs types all FK/`id` attributes as `int | None`** (unsaved models), and query filters (`.filter(folder_id__isnull=False)`) do **not** narrow the iterated object's attribute. Worse, `local_partial_types = true` in `pyproject.toml` prevents control-flow narrowing of those attributes via `assert`, `cast`, `if x is None: continue`, or local-variable binding — **none of them work.** Bulletproof fix: use `.values_list("col", flat=True)` to get plain `int`s, or drive the loop from the parent (saved) object's `id`. (Learned via `sync_channel_colors.py`.)
- mypy error moves to a _new_ line after each fix — read the current line number in the CI/vagrant output, don't assume it's the old line. Verify in Vagrant iteratively (`./tools/lint --groups=backend`), not by reasoning.

**assertLogs Pitfall:**

- `assertLogs("zulip.registration")` catches logs from `logging.getLogger("zulip.registration")`
- The logger name is whatever string was passed to `getLogger()`, NOT the file path
- Always grep for `logger = logging.getLogger(` in the target file to find the actual name

**Test State Collisions:**

- Django tests share database state within a test class
- If test A creates a user with email `x@zulip.com`, test B using the same email may get the existing user instead of creating a new one
- Use unique, descriptive emails: `"syncfail@zulip.com"`, `"portalcustom@zulip.com"`, etc.

**CI Transient Failures:**

- GitHub Docker Hub pulls can fail with 504 — just re-run
- tusd download 504s — just re-run
- These are infrastructure issues, not code problems

**Squash Workflow:**

```bash
# After making fixes, amend into the single commit:
git add -A
git commit --amend --no-edit
git push --force-with-lease origin portal-12.x
```
