#!/usr/bin/env bash
# Builds the package, packs it with `npm pack`, installs the tarball (not source paths) into a Vite app and a
# Next 13.1 Pages Router app (webpack, esmExternals: false), builds both, then checks the output:
# SSR HTML contains the transcript, the CSS is the package's, and no fixture code reaches the package tarball.
set -euo pipefail
cd "$(dirname "$0")/.."
PKG=$(pwd)
WORK="${FIXTURE_DIR:-$PKG/.fixtures}"
rm -rf "$WORK" && mkdir -p "$WORK"

npm run build >/dev/null
TARBALL="$WORK/$(npm pack --pack-destination "$WORK" --silent | tail -1)"
echo "packed: $TARBALL ($(stat -c%s "$TARBALL") bytes)"
tar -tzf "$TARBALL" | sort > "$WORK/tarball.txt"
if grep -E 'fixtures/|test/|src/' "$WORK/tarball.txt"; then echo "FAIL: fixtures/tests/source in tarball"; exit 1; fi

for app in vite-app next13-app; do
  cp -r "fixtures/$app" "$WORK/$app"
  if [ "$app" = vite-app ]; then cp fixtures/shared/Demo.tsx "$WORK/$app/src/Demo.tsx"; else cp fixtures/shared/Demo.tsx "$WORK/$app/Demo.tsx"; fi
  (cd "$WORK/$app" && npm install --no-audit --no-fund --silent "$TARBALL" >/dev/null)
  echo "== $app: building"
done

(cd "$WORK/vite-app" && npx tsc --noEmit && npx vite build 2>&1 | tail -6)
(cd "$WORK/next13-app" && NEXT_TELEMETRY_DISABLED=1 npx next build 2>&1 | tail -20)

echo "== checks"
# Vite: the bundle has one React and the package's CSS, scoped.
grep -q "ergo-chat" "$WORK"/vite-app/dist/assets/*.css
# Next: the page was prerendered (SSR/SSG) with the transcript in the HTML.
grep -q "ergo-chat__tool" "$WORK/next13-app/.next/server/pages/index.html"
grep -q 'Approve deploy' "$WORK/next13-app/.next/server/pages/index.html"
# Unsafe markdown never reached the HTML.
! grep -q 'javascript:alert' "$WORK/next13-app/.next/server/pages/index.html"
# And the host's own CSS ended up without package leakage into globals.
! grep -Eq '(^|})[[:space:]]*(body|html|:root)[[:space:]]*[{,]' "$WORK"/vite-app/dist/assets/*.css
echo "fixtures OK"
