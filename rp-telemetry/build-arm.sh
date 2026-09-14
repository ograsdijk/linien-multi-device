#!/usr/bin/env bash
#
# Cross-compile rp-telemetry for the Red Pitaya (armv7 / Zynq-7010) using
# Docker, so no ARM toolchain has to be installed on the developer machine.
# Works from Linux, macOS, and Windows (Git Bash / WSL) as long as the Docker
# daemon is running.
#
#   ./build-arm.sh
#
# Output:
#   rp-telemetry/build/rp-telemetry-armv7
#   linien-gateway/app/assets/rp-telemetry-armv7   (what the gateway deploys)
#
# The binary is linked statically: the Red Pitaya images in the field carry
# several different glibc versions, and a static build removes that variable
# entirely for ~700 KB of disk that the board has to spare.

set -euo pipefail

# Git Bash rewrites arguments that look like POSIX paths into Windows paths,
# which mangles the container-side paths below (-w /work becomes C:/.../work).
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bundle_dir="${here}/../linien-gateway/app/assets"

mkdir -p "${here}/build" "${bundle_dir}"

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

cp "${here}/build/rp-telemetry-armv7" "${bundle_dir}/rp-telemetry-armv7"

echo
echo "Built: ${here}/build/rp-telemetry-armv7"
echo "Bundled: ${bundle_dir}/rp-telemetry-armv7"
file "${bundle_dir}/rp-telemetry-armv7" 2>/dev/null || true
