# Bundled Red Pitaya assets

`rp-telemetry-armv7` — the cross-compiled `rp-telemetry` daemon the gateway
uploads to a Red Pitaya when an operator runs **Install telemetry**. It is not
checked in as a build artifact of this directory: build it from source with

```bash
cd rp-telemetry
./build-arm.sh          # Docker-based armv7 cross build, no local toolchain
# or, with a toolchain installed:
make bundle
```

Both commands drop the statically linked binary here. Until it exists, the
install action fails with a message pointing at this file rather than
deploying anything.

See the repo README section "Red Pitaya telemetry" for the full picture.
