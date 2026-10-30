# ==============================================================================
# Async_Tech_Sweep -- characterize simple asynchronous building blocks
# ------------------------------------------------------------------------------
# For every module in src/ this synthesizes it for a target technology, simulates
# it back-annotated, and records energy / delay / area. Sweeping the template
# parameters gives you the scaling curve of each block in that technology.
#
#   make sweep        # everything (slow: synthesis dominates)
#   make quick        # small subset, to check the flow works
#   make plot         # render sim/plots/*.png from sim/csv/*.csv
#   make all          # quick + plot
# ==============================================================================

SHELL   := /usr/bin/env bash
PYTHON  ?= python3

# Only `generic` is usable here: sky130 ships no synth.conf, and tsmc65 is not
# installed. Override once a real PDK is on the machine:  make sweep TECH=tsmc65
TECH    ?= generic

# Tokens generated per source, per parameter point.
N       ?= 30

# Per-tool wall-clock limit. Wide multipliers are the slow case.
TIMEOUT ?= 1800

# MODULE=<name> restricts the run to one module (repeat by rerunning).
MODULE  ?=
ifeq ($(strip $(MODULE)),)
  MODULE_ARG =
else
  MODULE_ARG = --module $(MODULE)
endif

# AREA=0 skips the dataflow+dflowmap pass that measures cell area.
AREA    ?= 1
ifeq ($(AREA),0)
  AREA_ARG = --no-area
else
  AREA_ARG =
endif

SWEEP = $(PYTHON) scripts/sweep.py --tech $(TECH) -n $(N) --timeout $(TIMEOUT) \
        $(MODULE_ARG) $(AREA_ARG)

.DEFAULT_GOAL := help
.PHONY: help sweep quick plot all list clean distclean

help: # Show this help
	@echo ""
	@echo "Async_Tech_Sweep -- asynchronous building-block characterization"
	@echo ""
	@awk 'BEGIN {FS = ":.*?# "} /^[a-zA-Z0-9_-]+:.*?# / \
	     { printf "  \033[1;32m%-12s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""
	@echo "  Variables (make <target> VAR=value):"
	@printf "    %-10s %s\n" "TECH"    "technology config        (default: $(TECH))"
	@printf "    %-10s %s\n" "N"       "inputs per source        (default: $(N))"
	@printf "    %-10s %s\n" "MODULE"  "restrict to one module   (default: all)"
	@printf "    %-10s %s\n" "AREA"    "0 = skip the area pass   (default: $(AREA))"
	@printf "    %-10s %s\n" "TIMEOUT" "per-tool seconds         (default: $(TIMEOUT))"
	@echo ""

sweep: # Full sweep of every module -> sim/csv/*.csv
	@$(SWEEP)

quick: # Small subset of every module, to smoke-test the flow
	@$(SWEEP) --quick

plot: # Render sim/plots/*.png from sim/csv/*.csv
	@$(PYTHON) scripts/plot.py

all: quick plot # Quick sweep, then plot

list: # List the modules that can be swept
	@$(PYTHON) -c "import sys; sys.path.insert(0,'scripts'); \
	 import sweep; [print('  ', m, len(d['points']), 'points') \
	 for m, d in sweep.MODULES.items()]"

clean: # Remove the per-run synthesis/simulation work dirs
	@rm -rf sim/work
	@echo "removed sim/work"

distclean: clean # Also remove the CSVs and plots
	@rm -rf sim/plots sim/csv
	@echo "removed sim/csv and sim/plots"
