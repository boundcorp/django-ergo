# Development

## Setup

```bash
make env               # uv virtualenv, dev requirements, pre-commit hooks
source .venv/bin/activate
pip install -e '.[dev]'
```

PostgreSQL comes from pgserver (an embedded server) unless `DATABASE_URL`
is set, so tests need nothing else running. To use your own database, copy
`.env.example` to `.env` and set `DATABASE_URL` to a database with the
pgvector extension.

Ergonaut has its own environment: `cd ergonaut && make venv`, which
installs Ergo editable from the repo root. See
[Running Ergonaut](ergonaut.md#development).

## Tests

```bash
make pytest                                 # everything, stop at the first failure
pytest tests/test_bot_schedules.py -v       # one file
make coverage && make open_coverage
cd ergonaut && make test                    # Ergonaut's tests
```

Tests run with `--ds=tests.example_app.settings`. Bot tests build bot
folders in `tmp_path` and use fake model clients, so they make no API calls.

OpenAI tests come in two tiers. `make tests_openai_real` calls the API
(`TEST_OPENAI=true`, costs credits) and saves fixtures under
`tests/fixtures/openai/`; `make tests_openai_mocked` replays them
(`tests/openai_test_utils.py`).

## Lint and format

```bash
make ruff_format
make ruff_check
pre-commit run --all-files
```

Pre-commit runs ruff on Python and prettier on `ergonaut/frontend`. Every
hook passes on main; don't commit with hooks skipped.

## CI

- `.github/workflows/test.yml`: Ergo's tests on pgserver and a migration
  drift check, on pull requests. On pushes it runs only an installed-wheel
  check.
- `.github/workflows/ergonaut.yml`: Ergonaut's tests, on pull requests. On
  pushes (and by hand) it builds the `release` image and starts it with
  `examples/hello` as a smoke test. On main, once the smoke test passes, it
  pushes the image as `sha-<commit>` and moves the `main` tag to it.

A merge to main doesn't rerun the test suites: the pull request already ran
them, and agents test before they push. Both workflows also run nightly on
main (and by hand), which catches pull requests that passed alone but break
together.

On a pull request, a newer push cancels the older run. On main a running
build is never cancelled: GitHub keeps at most one run waiting and replaces
it with the newest push, so a burst of merges builds the commit already
running and then only the latest one.

Tests and the wheel check run on GitHub-hosted runners, which are free for
this public repository and several times faster than our self-hosted ones.
The image job runs on the self-hosted runners; if they are down, set the
repository variable `CI_GITHUB_HOSTED` to `true` and it runs on
GitHub-hosted runners until the variable is removed. Pull requests from
forks need a maintainer's approval to start.

Image builds keep their Docker layers in the GitHub Actions cache (scope
`ergonaut`), and Python installs
use the pip and uv caches, so a fresh runner doesn't start from scratch.

## Conventions

- Docs change with the code. New or changed bot.yaml keys, plugin options,
  tools or Ergonaut settings go in [bots.md](bots.md) and the relevant
  guide in the same PR.
- Tools Ergo provides are named `ergo_*`.
- Migrations: `make migrations`, and check the diff. Bot table migrations
  live in each bot folder, not here.
- [TODO.md](../TODO.md) tracks the bots and Ergonaut backlog.
- Releases are reviewed commits on `main`; see [git-release.md](git-release.md).
