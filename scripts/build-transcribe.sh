#!/usr/bin/env bash
# Speech-to-text runtime for the meeting follower (spec 044), into the miniteams cache dir:
#   parakeet: libtranscribe (transcribe.cpp, pinned, AVX2) + the parakeet-ultra GGUF (sha256-checked)
#   whisper:  faster-whisper "small" at a pinned revision (model.bin sha256-checked)
# Usage: scripts/build-transcribe.sh [parakeet|whisper|all]   (default: all)
# Env: MINITEAMS_CACHE_DIR (default ${XDG_CACHE_HOME:-~/.cache}/miniteams), BUILD_JOBS (default 2). The
# environment only, never `.env`: `miniteams follow` prints the command with the cache dir it uses.
# Builds in Docker (debian:trixie-slim, pinned by digest) when docker is available, else natively (cmake,
# git, a C++ compiler).
# Each step is skipped when its output is already in place.
set -euo pipefail

readonly TCPP_REPO="https://github.com/handy-computer/transcribe.cpp.git"
readonly TCPP_COMMIT="f1dcd15858c4bbba87ca97862b4e448b2cf15b8f"  # pragma: allowlist secret (a pin, not a secret)
readonly PARAKEET_REPO="handy-computer/parakeet-ultra-gguf"
readonly PARAKEET_REVISION="39eeb55181f0d354fd934f06e92fd8d5037fed8e"  # pragma: allowlist secret (a pin, not a secret)
readonly PARAKEET_FILE="parakeet-ultra-Q8_0.gguf"
readonly PARAKEET_SHA256="007a59761e9258779f189df396b50d12b83bb5b79e66df4b955c230d2f2a0a59"  # pragma: allowlist secret (a pin, not a secret)
readonly WHISPER_MODEL="small"
readonly WHISPER_REVISION="536b0662742c02347bc0e980a01041f333bce120"  # pragma: allowlist secret (a pin, not a secret)
readonly WHISPER_SHA256="3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"  # pragma: allowlist secret (a pin, not a secret)
readonly BUILD_IMAGE="debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2"  # pragma: allowlist secret (a pin, not a secret)

readonly CACHE="${MINITEAMS_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/miniteams}"
readonly JOBS="${BUILD_JOBS:-2}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT

# Never GGML_NATIVE: a library built on a newer CPU would die with SIGILL on an AVX2-only one.
# $ORIGIN: libtranscribe finds its ggml libraries beside it. Every path comes from the environment
# (SRC_DIR, OUT_DIR), never spliced into the script text.
# shellcheck disable=SC2016  # expanded by the inner bash, from its environment
readonly BUILD_STEPS='
set -euo pipefail
git init -q "$SRC_DIR" && cd "$SRC_DIR"
git fetch -q --depth 1 "$TCPP_REPO" "$TCPP_COMMIT" && git checkout -q FETCH_HEAD
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
  -DTRANSCRIBE_BUILD_SHARED=ON -DTRANSCRIBE_BUILD_TESTS=OFF -DTRANSCRIBE_BUILD_EXAMPLES=OFF \
  -DTRANSCRIBE_INSTALL=OFF -DTRANSCRIBE_USE_SYSTEM_BLAS=OFF \
  -DGGML_NATIVE=OFF -DGGML_AVX=ON -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON -DGGML_BMI2=ON \
  -DGGML_AVX_VNNI=OFF -DGGML_AVX512=OFF \
  -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON "-DCMAKE_INSTALL_RPATH=\$ORIGIN" >/dev/null
cmake --build build -j "$BUILD_JOBS" --target transcribe
mkdir -p "$OUT_DIR/lib" "$OUT_DIR/licenses"
find build -name "*.so*" -exec cp -a {} "$OUT_DIR/lib/" \;
cp LICENSE THIRD-PARTY-LICENSES.md "$OUT_DIR/licenses/"
cp ggml/LICENSE "$OUT_DIR/licenses/LICENSE.ggml"
test -f "$OUT_DIR/lib/libtranscribe.so"
'
# The container runs as root, the cache belongs to the caller: OWNER gets the output back.
# shellcheck disable=SC2016  # expanded by the container's bash, from its environment
readonly DOCKER_STEPS='
set -euo pipefail
apt-get update -qq
apt-get install -y -qq --no-install-recommends build-essential cmake git ca-certificates >/dev/null
bash -c "$BUILD_STEPS"
chown -R "$OWNER" "$OUT_DIR"
'
# Global: the EXIT trap runs after build_lib returns, when its locals are gone.
BUILD_OUT=""

die() {
    echo "build-transcribe: $*" >&2
    exit 1
}

build_lib() {
    local dest="$CACHE/transcribe"
    # No COMMIT file yet = not built: cat's failure is that answer.
    if [[ "$(cat "$dest/COMMIT" 2>/dev/null || true)" == "$TCPP_COMMIT" && -f "$dest/lib/libtranscribe.so" ]]; then
        echo "libtranscribe @ ${TCPP_COMMIT:0:7} already in $dest/lib"
        return
    fi
    BUILD_OUT="$(mktemp -d "$CACHE/.transcribe.XXXXXX")"
    trap 'rm -rf "$BUILD_OUT"' EXIT
    if command -v docker >/dev/null; then
        echo "building libtranscribe @ ${TCPP_COMMIT:0:7} in docker, $JOBS jobs"
        docker run --rm -v "$BUILD_OUT:/out" \
            -e TCPP_REPO="$TCPP_REPO" -e TCPP_COMMIT="$TCPP_COMMIT" -e BUILD_JOBS="$JOBS" \
            -e SRC_DIR=/src -e OUT_DIR=/out -e BUILD_STEPS="$BUILD_STEPS" -e OWNER="$(id -u):$(id -g)" \
            "$BUILD_IMAGE" bash -c "$DOCKER_STEPS" ||
            die "docker build failed"
    else
        local tool
        for tool in git cmake c++; do
            command -v "$tool" >/dev/null || die "$tool missing: install it, or docker"
        done
        echo "building libtranscribe @ ${TCPP_COMMIT:0:7} natively, $JOBS jobs"
        TCPP_REPO="$TCPP_REPO" TCPP_COMMIT="$TCPP_COMMIT" BUILD_JOBS="$JOBS" \
            SRC_DIR="$BUILD_OUT/src" OUT_DIR="$BUILD_OUT" bash -c "$BUILD_STEPS" || die "native build failed"
    fi
    rm -rf "$dest"
    mkdir -p "$dest"
    mv "$BUILD_OUT/lib" "$BUILD_OUT/licenses" "$dest/"
    echo "$TCPP_COMMIT" >"$dest/COMMIT"
    echo "libtranscribe in $dest/lib"
}

fetch_parakeet() {
    local dir="$CACHE/models/parakeet"
    local file="$dir/$PARAKEET_FILE"
    # A partial or corrupt file fails the check and is fetched again.
    if [[ -f "$file" ]] && echo "$PARAKEET_SHA256  $file" | sha256sum -c --quiet - 2>/dev/null; then
        echo "parakeet model already in $file"
        return
    fi
    mkdir -p "$dir"
    local part="$file.part"
    echo "fetching $PARAKEET_FILE @ ${PARAKEET_REVISION:0:7}"
    curl -fL --retry 3 --connect-timeout 20 -o "$part" \
        "https://huggingface.co/$PARAKEET_REPO/resolve/$PARAKEET_REVISION/$PARAKEET_FILE" ||
        die "download of $PARAKEET_FILE failed"
    if ! echo "$PARAKEET_SHA256  $part" | sha256sum -c --quiet -; then
        rm -f "$part"
        die "$PARAKEET_FILE: sha256 mismatch, file removed"
    fi
    mv "$part" "$file"
    echo "parakeet model in $file"
}

whisper_ok() {
    local dir="$1"
    # No REVISION file yet = not fetched: cat's failure is that answer. A corrupt model.bin fails the sum.
    [[ "$(cat "$dir/REVISION" 2>/dev/null || true)" == "$WHISPER_REVISION" ]] &&
        echo "$WHISPER_SHA256  $dir/model.bin" | sha256sum -c --quiet - 2>/dev/null
}

fetch_whisper() {
    local dir="$CACHE/models/whisper-small"
    if whisper_ok "$dir"; then
        echo "whisper $WHISPER_MODEL already in $dir"
        return
    fi
    echo "fetching whisper $WHISPER_MODEL @ ${WHISPER_REVISION:0:7}"
    rm -f "$dir/REVISION"
    uv run --project "$REPO_ROOT" --extra transcript python -I -c \
        "import sys; from faster_whisper import download_model; \
download_model(sys.argv[1], output_dir=sys.argv[2], revision=sys.argv[3])" \
        "$WHISPER_MODEL" "$dir" "$WHISPER_REVISION" || die "whisper download failed"
    if ! echo "$WHISPER_SHA256  $dir/model.bin" | sha256sum -c --quiet -; then
        rm -f "$dir/model.bin"
        die "whisper model.bin: sha256 mismatch, file removed"
    fi
    echo "$WHISPER_REVISION" >"$dir/REVISION"
    echo "whisper model in $dir"
}

main() {
    local what="${1:-all}"
    case "$what" in
    parakeet | whisper | all) ;;
    *)
        echo "usage: $0 [parakeet|whisper|all]" >&2
        exit 2
        ;;
    esac
    mkdir -p "$CACHE"
    if [[ "$what" != whisper ]]; then
        build_lib
        fetch_parakeet
    fi
    if [[ "$what" != parakeet ]]; then
        fetch_whisper
    fi
}

main "$@"
