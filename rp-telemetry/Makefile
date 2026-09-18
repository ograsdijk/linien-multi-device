# rp-telemetry build.
#
#   make                 -> native build into build/rp-telemetry (for tests)
#   make arm             -> armv7 cross build (needs arm-linux-gnueabihf-gcc)
#   make bundle          -> arm build, copied into the gateway's assets dir
#   make clean
#
# The ARM build targets the Zynq-7010 on the Gen 1 STEMlab 125-14 and is
# linked statically so it does not depend on the glibc version of whatever
# Red Pitaya image the board runs.

CC      ?= cc
CFLAGS  ?= -O2 -Wall -Wextra -std=c99 -D_DEFAULT_SOURCE
SRC      = src/rp_telemetry.c
BUILD    = build

ARM_CC     ?= arm-linux-gnueabihf-gcc
ARM_CFLAGS ?= -O2 -Wall -Wextra -std=c99 -D_DEFAULT_SOURCE \
              -march=armv7-a -mfpu=neon -mfloat-abi=hard -static

# Where the gateway looks for the binary it deploys.
BUNDLE_DIR = ../linien-gateway/app/assets
BUNDLE     = $(BUNDLE_DIR)/rp-telemetry-armv7

.PHONY: all arm bundle clean

all: $(BUILD)/rp-telemetry

$(BUILD)/rp-telemetry: $(SRC)
	@mkdir -p $(BUILD)
	$(CC) $(CFLAGS) -o $@ $(SRC)

arm: $(BUILD)/rp-telemetry-armv7

$(BUILD)/rp-telemetry-armv7: $(SRC)
	@mkdir -p $(BUILD)
	$(ARM_CC) $(ARM_CFLAGS) -o $@ $(SRC)
	-arm-linux-gnueabihf-strip $@ 2>/dev/null || true

bundle: $(BUILD)/rp-telemetry-armv7
	@mkdir -p $(BUNDLE_DIR)
	cp $(BUILD)/rp-telemetry-armv7 $(BUNDLE)
	@echo "bundled -> $(BUNDLE)"

clean:
	rm -rf $(BUILD)
