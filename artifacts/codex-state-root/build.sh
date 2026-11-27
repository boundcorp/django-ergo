#!/bin/sh
# Reproduce the CODEX_RUNTIME_HOME candidate build used for e2e-results.json.
#
# Everything (toolchain, cargo registry, OpenSSL headers, rusty_v8 prebuilt,
# target dir) lives under $B, a git-ignored folder of this worktree. Nothing is
# installed system-wide or into a shared PATH. Parallelism is capped at 2.
#
#   sh artifacts/codex-state-root/build.sh [build-dir]
#   python3 -I artifacts/codex-state-root/e2e.py \
#     --binary  <build-dir>/bin/codex-0.171.0-runtime-home \
#     --baseline <build-dir>/bin/codex-0.171.0-baseline \
#     --fixture-root <build-dir>/fixtures
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
B=$(mkdir -p "${1:-$HERE/../../build/codex-state-root}" && cd "${1:-$HERE/../../build/codex-state-root}" && pwd)

# Pins (see ../codex-state-root-build.md).
CODEX_TAG=rust-v0.171.0
CODEX_COMMIT=ccf9be8cc458bd6b8336fe84d68d8893f2691a5e
RUST=1.99.1                       # codex-rs/rust-toolchain.toml at that commit
V8=152.2.0                        # workspace `v8` pin
V8_PROFILE=ptrcomp_sandbox_release
TARGET=x86_64-unknown-linux-gnu
SSL_DEB='libssl-dev_3.0.22-1~deb12u2_amd64.deb'
SSL_SHA=c5f948bf6cd6e8e6e8539364bed3280ff67bc2e058de484a1f6cce8d5afcc90e
PATCH_SHA=506c1e8ba9b8cfeef8814edb0ffc0cf5f08e13214b2ed9d63ad312f9b0cd722e

export RUSTUP_HOME=$B/rustup CARGO_HOME=$B/cargo-home CARGO_TARGET_DIR=$B/target
export PATH=$CARGO_HOME/bin:/usr/bin:/bin:/usr/local/bin
export CARGO_BUILD_JOBS=2 CARGO_INCREMENTAL=0 CARGO_PROFILE_DEV_DEBUG=0

echo "$PATCH_SHA  $HERE/runtime-home.patch" | sha256sum -c -

# 1. Official source at the pinned commit.
[ -d "$B/src/.git" ] || git clone -q --depth 1 --branch "$CODEX_TAG" https://github.com/openai/codex.git "$B/src"
[ "$(git -C "$B/src" rev-parse HEAD)" = "$CODEX_COMMIT" ]

# 2. Toolchain, local to $B.
if [ ! -x "$CARGO_HOME/bin/rustup" ]; then
  curl -sSf https://sh.rustup.rs -o "$B/rustup-init.sh"
  sh "$B/rustup-init.sh" -y --no-modify-path --default-toolchain none --profile minimal
fi
(cd "$B/src/codex-rs" && rustup toolchain install "$RUST" --profile minimal -c rustfmt)

# 3. OpenSSL headers/static libs for openssl-sys (host has no -dev package or sudo).
if [ ! -d "$B/ossl" ]; then
  mkdir -p "$B/debs" "$B/sysroot" "$B/ossl/include" "$B/ossl/lib"
  curl -fsSL -o "$B/debs/$SSL_DEB" "https://deb.debian.org/debian-security/pool/updates/main/o/openssl/$SSL_DEB"
  echo "$SSL_SHA  $B/debs/$SSL_DEB" | sha256sum -c -
  dpkg -x "$B/debs/$SSL_DEB" "$B/sysroot"
  cp -r "$B/sysroot/usr/include/openssl" "$B/ossl/include/"
  cp "$B/sysroot/usr/include/x86_64-linux-gnu/openssl/"* "$B/ossl/include/openssl/"
  cp "$B/sysroot/usr/lib/x86_64-linux-gnu/"*.a "$B/ossl/lib/"
fi
export OPENSSL_INCLUDE_DIR=$B/ossl/include OPENSSL_LIB_DIR=$B/ossl/lib OPENSSL_STATIC=1

# 4. Codex-built rusty_v8 prebuilt, verified like .github/actions/setup-rusty-v8.
D=$B/rusty_v8; mkdir -p "$D"
base=https://github.com/openai/codex/releases/download/rusty-v8-v$V8
cs=rusty_v8_${V8_PROFILE}_$TARGET.sha256
if [ ! -f "$D/$cs" ]; then
  curl -fsSL "$base/$cs" -o "$D/$cs"
  want=$(grep -F "  $cs" "$B/src/third_party/v8/rusty_v8_$(echo $V8 | tr . _)_release_manifests.sha256" | cut -d' ' -f1)
  [ -n "$want" ] && [ "$(sha256sum "$D/$cs" | cut -d' ' -f1)" = "$want" ]
  curl -fsSL "$base/librusty_v8_${V8_PROFILE}_$TARGET.a.gz" -o "$D/librusty_v8_${V8_PROFILE}_$TARGET.a.gz"
  curl -fsSL "$base/src_binding_${V8_PROFILE}_$TARGET.rs" -o "$D/src_binding_${V8_PROFILE}_$TARGET.rs"
fi
(cd "$D" && sha256sum -c "$cs")
export RUSTY_V8_ARCHIVE=$D/librusty_v8_${V8_PROFILE}_$TARGET.a.gz
export RUSTY_V8_SRC_BINDING_PATH=$D/src_binding_${V8_PROFILE}_$TARGET.rs

# 5. Baseline, then candidate (patch applied in place so only touched crates rebuild).
#    No --locked: the tag's Cargo.lock lists workspace crates as 0.0.0 while
#    Cargo.toml says 0.171.0; cargo rewrites only those versions (plus the
#    patch's new codex-utils-home-dir edges).
mkdir -p "$B/bin"
cd "$B/src/codex-rs"
if ! git -C "$B/src" apply --reverse --check "$HERE/runtime-home.patch" 2>/dev/null; then
  cargo build -p codex-cli --bin codex
  cp "$CARGO_TARGET_DIR/debug/codex" "$B/bin/codex-0.171.0-baseline"
  git -C "$B/src" apply "$HERE/runtime-home.patch"
fi
cargo build -p codex-cli --bin codex
cp "$CARGO_TARGET_DIR/debug/codex" "$B/bin/codex-0.171.0-runtime-home"
sha256sum "$B/bin/"*
