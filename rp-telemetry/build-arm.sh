#!/usr/bin/env bash
#
# Cross-compile rp-telemetry for the Red Pitaya (armv7 / Zynq-7010) without
# installing an ARM toolchain on the developer machine. Two backends, picked
# automatically:
#
#   docker  the reference build (Debian + gcc-arm-linux-gnueabihf, glibc)
#   zig     a fallback for machines with no container runtime (musl)
#
# Docker wins when its daemon is reachable. Override with BUILD_BACKEND=zig or
# BUILD_BACKEND=docker.
#
#   ./build-arm.sh
#
# Output:
#   rp-telemetry/build/rp-telemetry-armv7
#   linien-gateway/app/assets/rp-telemetry-armv7   (what the gateway deploys)
#
# Either backend links statically: the Red Pitaya images in the field carry
# several different glibc versions, and a static build removes that variable
# entirely for disk the board has to spare. The daemon only uses file I/O and
# sockets -- no NSS, locale, or dlopen -- so the two libcs are interchangeable
# here, and a static musl build avoids the NSS caveats of a static glibc one.
#
# After building, bump-check the result: the gateway refuses to consider a
# board up to date unless the bundled binary really contains BUNDLED_VERSION
# (tests/test_rp_telemetry_protocol.py guards this). A stale asset shows up as
# an "update available" banner that reinstalling never clears.

set -euo pipefail

# Git Bash rewrites arguments that look like POSIX paths into Windows paths,
# which mangles the container-side paths below (-w /work becomes C:/.../work).
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bundle_dir="${here}/../linien-gateway/app/assets"

mkdir -p "${here}/build" "${bundle_dir}"

backend="${BUILD_BACKEND:-}"
if [ -z "${backend}" ]; then
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    backend="docker"
  elif command -v zig >/dev/null 2>&1; then
    backend="zig"
  else
    echo "error: need either a running Docker daemon or zig on PATH." >&2
    echo "       Docker:  install Docker Desktop / colima, then start it" >&2
    echo "       zig:     brew install zig   (or https://ziglang.org/download/)" >&2
    exit 1
  fi
fi

case "${backend}" in
docker)
  # Docker Desktop on Windows needs a native path for the bind mount; `pwd -W`
  # yields one under Git Bash and does not exist elsewhere.
  mount_src="${here}"
  if (cd "${here}" && pwd -W) >/dev/null 2>&1; then
    mount_src="$(cd "${here}" && pwd -W)"
  fi

  image="debian:bookworm"

  docker run --rm \
    -v "${mount_src}:/work" \
    -w /work \
    "${image}" \
    bash -euo pipefail -c '
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq
      apt-get install -y -qq --no-install-recommends \
        gcc-arm-linux-gnueabihf libc6-dev-armhf-cross binutils-arm-linux-gnueabihf
      arm-linux-gnueabihf-gcc \
        -O2 -Wall -Wextra -std=c99 -D_DEFAULT_SOURCE \
        -march=armv7-a -mfpu=neon -mfloat-abi=hard -static \
        -o build/rp-telemetry-armv7 src/rp_telemetry.c
      arm-linux-gnueabihf-strip build/rp-telemetry-armv7
    '
  ;;
zig)
  # Zig carries its own cross headers and libc sources, so this needs nothing
  # but the zig binary. cortex_a9+neon matches the Zynq-7010; musleabihf keeps
  # the hard-float ABI the Red Pitaya userspace uses.
  (
    cd "${here}"
    zig cc \
      -target arm-linux-musleabihf -mcpu=cortex_a9+neon \
      -O2 -Wall -Wextra -std=c99 -D_DEFAULT_SOURCE -static \
      -o build/rp-telemetry-armv7 src/rp_telemetry.c
  )
  # Zig bundles llvm-strip under the same version of LLVM it was built with;
  # fall back to whatever strip understands ARM ELF if it is not on PATH.
  if command -v llvm-strip >/dev/null 2>&1; then
    llvm-strip "${here}/build/rp-telemetry-armv7"
  elif [ -x /opt/homebrew/opt/llvm@21/bin/llvm-strip ]; then
    /opt/homebrew/opt/llvm@21/bin/llvm-strip "${here}/build/rp-telemetry-armv7"
  else
    echo "note: no llvm-strip found; shipping an unstripped binary." >&2
  fi
  ;;
*)
  echo "error: unknown BUILD_BACKEND '${backend}' (expected docker or zig)." >&2
  exit 1
  ;;
esac

cp "${here}/build/rp-telemetry-armv7" "${bundle_dir}/rp-telemetry-armv7"

echo
echo "Backend: ${backend}"
echo "Built: ${here}/build/rp-telemetry-armv7"
echo "Bundled: ${bundle_dir}/rp-telemetry-armv7"
file "${bundle_dir}/rp-telemetry-armv7" 2>/dev/null || true
