# ==============================================================================
# Async_Tech_Sweep -- characterize simple asynchronous building blocks
# ------------------------------------------------------------------------------
# For every module in src/ this synthesizes it for a target technology, simulates
# it back-annotated, and records energy / delay. Sweeping the template
# parameters gives you the scaling curve of each block in that technology.
#
#   make sweep        # everything (slow: synthesis dominates)
#   make sweep JOBS=1 # ... one point at a time (default: 8 at once)
#   make quick        # every curve, secondary axes pinned to one value
#   make plot         # render sim/plots/*.png from sim/csv/*.csv
#   make all          # quick + plot
#
# A sweep is three steps:
#   1. scripts/sweep.py gen      writes one dir per parameter point under
#                                sim/work/ (test bench + actsim script)
#   2. make points               runs the tools below in every point dir
#   3. scripts/sweep.py collect  parses the point dirs into sim/csv/*.csv
# ==============================================================================

SHELL   := /usr/bin/env bash

# Python deps (matplotlib, pandas) live in a repo-local venv, built on demand
# from requirements.txt. The base is the system /usr/bin/python3.11 on purpose:
# a venv built on the cluster's module Python cannot find libpython3.10.so
# unless that module is loaded, so it breaks in editors and batch jobs.
VENV      := .venv
VENV_BASE ?= /usr/bin/python3.11
PYTHON    ?= $(VENV)/bin/python
VENV_OK   := $(VENV)/.installed

# Technology config from $ACT_HOME/conf. Only the ones with a synth.conf can be
# synthesized -- here generic and tsmc65 (`make help` lists them).
TECH    ?= generic

# Tokens each source sends, per parameter point. The CSV/plots report totals
# divided by the operation count, i.e. the average cost of ONE operation; with
# a random control (cond_select random) that average includes the activity.
N       ?= 30

# Per-tool wall-clock limit. Wide multipliers are the slow case.
TIMEOUT ?= 1800

# Points run in parallel by `make sweep` / `make quick`.
JOBS    ?= 8

# MODULE=<name> restricts the run to one module (repeat by rerunning).
MODULE  ?=
ifeq ($(strip $(MODULE)),)
  MODULE_ARG =
else
  MODULE_ARG = --module $(MODULE)
endif

WORK    := sim/work

# The generated wrappers say `import "multiplier.act"`; ACT_PATH finds src/.
ifeq ($(findstring $(CURDIR)/src,$(ACT_PATH)),)
  export ACT_PATH := $(CURDIR)/src$(if $(ACT_PATH),:$(ACT_PATH))
endif

# ==============================================================================
# Tool commands
# ------------------------------------------------------------------------------
# Every command runs inside one point's dir, sim/work/<module>/<tag>/, where
# `sweep.py gen` has written dut.act (the `dut` wrapper + test bench `tb`),
# top_decomp.act (the bench around the decomposed `decomp_dut`) and run.scr.
# ==============================================================================

# 1. Decompose the DUT. Genus maps the expressions: it is the only backend that
#    fills the energy/leakage tables in decomp_sim.conf (the built-in abc leaves
#    them all 0). If Genus fails, abc is retried so delay is still measured.
SYNTH_DECOMP   = synth2 -T$(TECH) -o dut_decomp.act -F decomp -t
SYNTH_GENUS    = $(SYNTH_DECOMP) -E genus -cnf=custom.conf -p dut dut.act
SYNTH_ABC      = $(SYNTH_DECOMP) -p dut dut.act
SYNTH_CONF     = int synth.expropt.skip_verification 1

# 2. Simulate the decomposed DUT, back-annotated with decomp_sim.conf. run.scr
#    watches the outputs, runs, counts tokens and writes energy.txt.
SIM            = actsim -ref=1 -Wlang_subst:off -cnf=decomp_sim.conf top_decomp.act tb_d

# $(call run,<log>,<command>): run <command> under the timeout; its output goes
# to <log> (headed by the command line) and its wall time to <log>.time.
run = { echo '$(2)'; echo; /usr/bin/time -f %e -o $(basename $(1)).time \
        timeout $(TIMEOUT) $(2); } > $(1) 2>&1

# sim/work/adder/width8 -> adder/width8, for progress lines.
point = $(patsubst $(WORK)/%,%,$*)

# Arguments both sweep.py steps get, so they agree on the set of points.
SEL = -n $(N) $(MODULE_ARG) $(if $(filter quick,$@),--quick)

.DEFAULT_GOAL := help
.PHONY: help venv sweep quick points plot all list clean distclean

# For `make help` only (recursive `=`, so nothing runs unless help is asked for).
HELP_TECHS   = $(shell for d in "$$ACT_HOME"/conf/*/; do \
                 [ -f "$$d/synth.conf" ] && basename "$$d"; done)
HELP_MODULES = $(shell $(VENV_BASE) -c "import sys; sys.path.insert(0, 'scripts'); \
                 import sweep; print(*sweep.MODULES)")
HELP_VAR     = printf "    \033[1;36m%-10s\033[0m %-22s %s\n"

help: # Show this help
	@echo ""
	@echo "Async_Tech_Sweep -- asynchronous building-block characterization"
	@echo ""
	@echo "  Targets:"
	@awk 'BEGIN {FS = ":.*?# "} /^[a-zA-Z0-9_-]+:.*?# / \
	     { printf "    \033[1;32m%-10s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""
	@echo "  Variables (make <target> VAR=value):"
	@$(HELP_VAR) VARIABLE "CURRENT VALUE" "MEANING"
	@$(HELP_VAR) TECH      "$(TECH)"      "technology: $(HELP_TECHS)"
	@$(HELP_VAR) MODULE    "$(or $(MODULE),(all))" "sweep only this module (see below)"
	@$(HELP_VAR) N         "$(N)"         "tokens each source sends, per point"
	@$(HELP_VAR) JOBS      "$(JOBS)"      "points run in parallel"
	@$(HELP_VAR) TIMEOUT   "$(TIMEOUT)"   "seconds allowed per tool run"
	@$(HELP_VAR) PYTHON    "$(PYTHON)"    "interpreter for scripts/*.py"
	@$(HELP_VAR) VENV_BASE "$(VENV_BASE)" "python that \`make venv\` builds .venv from"
	@echo ""
	@echo "  Modules: $(HELP_MODULES)"
	@echo ""
	@echo "  Examples:"
	@echo "    make quick TECH=tsmc65"
	@echo "    make sweep MODULE=adder"
	@echo "    make sim/work/adder/width8/sim.done     # rerun one point"
	@echo ""

$(VENV_OK): requirements.txt
	@test -x $(VENV)/bin/python || $(VENV_BASE) -m venv $(VENV)
	@$(VENV)/bin/pip install -q --upgrade pip
	@$(VENV)/bin/pip install -q -r requirements.txt
	@touch $@
	@echo "venv ready: $(VENV)"

venv: $(VENV_OK) # Create/update .venv from requirements.txt

sweep: $(VENV_OK) # Full sweep of every module -> sim/csv/*.csv
quick: $(VENV_OK) # Every curve, secondary axes pinned, to smoke-test the flow
sweep quick:
	@$(PYTHON) scripts/sweep.py gen $(SEL)
	@$(MAKE) --no-print-directory -k -j$(JOBS) points || true
	@$(PYTHON) scripts/sweep.py collect $(SEL) --tech $(TECH)

# ==============================================================================
# Per-point rules. `make points` runs every dir listed by the last `gen`; one
# point can also be run by hand, e.g.  make sim/work/adder/width8/sim.done
# A stage touches its .done file only on success; `sweep.py collect` reads a
# missing one as SYNTH_FAIL / SIM_FAIL, and -k keeps the other points going.
# ==============================================================================

-include $(WORK)/points.mk

points: # Run synth + sim on the points from the last sweep/quick
points: $(addsuffix /sim.done,$(POINTS))

%/custom.conf:
	@echo '$(SYNTH_CONF)' > $@

%/synth.done: %/dut.act %/custom.conf
	@echo "  synth  $(point)"
	@cd $* && rm -f dut_decomp.act && \
	if $(call run,synth.log,$(SYNTH_GENUS)) && test -f dut_decomp.act; then \
	  echo genus > synth.backend; \
	else \
	  echo "  synth  $(point): genus failed, retrying with abc"; \
	  $(call run,synth_abc.log,$(SYNTH_ABC)) && test -f dut_decomp.act && \
	  echo abc > synth.backend; \
	fi
	@touch $@

%/sim.done: %/synth.done %/top_decomp.act %/run.scr
	@echo "  sim    $(point)"
	@cd $* && $(call run,sim.log,$(SIM) < run.scr)
	@touch $@

# The .done files are intermediates of `points`; keep them for collect.
.SECONDARY:

plot: $(VENV_OK) # Render sim/plots/*.png from sim/csv/*.csv
	@$(PYTHON) scripts/plot.py

all: quick plot # Quick sweep, then plot

list: $(VENV_OK) # List the modules that can be swept
	@$(PYTHON) -c "import sys; sys.path.insert(0,'scripts'); \
	 import sweep; [print('  ', m, len(d['points']), 'points') \
	 for m, d in sweep.MODULES.items()]"

clean: # Remove the per-run synthesis/simulation work dirs
	@rm -rf $(WORK)
	@echo "removed $(WORK)"

distclean: clean # Also remove the CSVs and plots
	@rm -rf sim/plots sim/csv
	@echo "removed sim/csv and sim/plots"
