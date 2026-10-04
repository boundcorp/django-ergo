# Ergonaut release-image toolchain: plan and report

Base: origin/main c7e79ac. Branch: leewardbound/ergonaut-release-toolchain.

## Plan
1. Release stage of `ergonaut/Dockerfile`: install git, openssh-client, gh (official apt repo),
   `bun` + `@anthropic-ai/claude-code` + `@oh-my-pi/pi-coding-agent` globally via npm (omp needs Bun),
   Orca as pinned AppImage (`ORCA_VERSION`, `ORCA_LINUX_APPIMAGE_SHA256` build args), sha256 verified
   before `--appimage-extract` into `/opt/orca`, symlink `/opt/orca/resources/bin/orca-ide` -> `/usr/local/bin/orca`,
   plus Electron runtime libs (modelled on devbox, which was not modified).
2. `aio` inherits `release`, so it gets the toolchain.
3. CI (`.github/workflows/ergonaut.yml`): new `publish` job, `push` to `main` only, `needs: [test, image]`,
   `packages: write` scoped to that job (workflow default stays `contents: read`), GITHUB_TOKEN login,
   concurrency group (no overlapping publishes), tags `main` and `sha-<sha>`. PRs never publish.
   Existing `test`/`image` jobs and compose dev stack untouched. `latest` not pushed (compose references
   `ghcr.io/boundcorp/ergonaut/release:latest`, a separate path).
4. Docs: `docs/ergonaut.md`.

## Verification (local Docker 29.8.2, network available)
- `docker build -f ergonaut/Dockerfile --target aio -t ergonaut:aio-local .` -> exit 0 (sha256 check passed).
- In image (user `ergonaut`): `orca --help` prints usage; `claude --version` 2.1.289; `omp --version` omp/18.6.0;
  `bun --version` 1.4.2; `gh --version` 2.102.0; git 2.47.3; ssh OpenSSH_10.0p2.
- First build revealed `omp` failed with "bun: No such file" -> added `bun` to npm install; fixed.
- CI aio smoke replicated: healthz OK, "Serving bots: hello", embedded Garage, celery ready, `ergonaut check` -> "All bots loaded".
  (The sandbox docker daemon could not see bind mounts, so the bot dir was `docker cp`'d instead of `-v`.)
- Workflow YAML parses (jobs: test, image, publish). The publish job itself can only run on GitHub after merge to main; not exercised.
- Python test suite not run (no Python code changed); CI `test` job covers it.

## Reproduce
    docker build -f ergonaut/Dockerfile --target aio -t ergonaut:aio-local .
    docker run --rm --entrypoint sh ergonaut:aio-local -c 'orca --help; claude --version; omp --version'
Bump Orca: `--build-arg ORCA_VERSION=... --build-arg ORCA_LINUX_APPIMAGE_SHA256=...`.

## Notes / risks
- Claude Code and omp are unpinned (latest at build time), matching devbox's latest-install approach.
- Image size grows (Electron libs + Orca ~ hundreds of MB).
- Orca `serve` headless was not exercised, only the CLI.

## Commit / PR
00bddde — https://github.com/boundcorp/django-ergo/pull/100 (not merged)
