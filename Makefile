# Copyright 2026 Creative Stack Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Creative Stack — see `make help`.
#
# Everything runs through the repo venv, not the system python, so a target
# behaves the same however your shell happens to be set up today.

SHELL   := /bin/bash
PY      := .venv/bin/python
SCRIPTS := scripts
OUT     := out

.DEFAULT_GOAL := help
.PHONY: help install headless doctor check plan host skills lint demo demo-clean \
        demo-verify brewfile verify clean distclean

## ---------------------------------------------------------------- setup ---

help:  ## show this help
	@echo ""
	@echo "  Creative Stack"
	@echo ""
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo ""

install:  ## full bootstrap (packages, venv, bridges, MCP, skills)
	bash $(SCRIPTS)/bootstrap.sh

headless:  ## bootstrap without desktop apps — servers, CI, containers
	bash $(SCRIPTS)/bootstrap.sh --headless --yes

## --------------------------------------------------------------- verify ---

doctor:  ## full verification: tools, bridges, credentials, MCP, skills
	$(PY) $(SCRIPTS)/doctor.py

check:  ## did every manifest package produce its binaries
	$(PY) $(SCRIPTS)/packages.py --check

plan:  ## what bootstrap would install on this machine, and by which route
	$(PY) $(SCRIPTS)/packages.py --plan

host:  ## what this machine looks like to the stack
	$(PY) $(SCRIPTS)/hostenv.py

lint:  ## validate skill structure and scan for committed secrets
	$(PY) $(SCRIPTS)/validate_skills.py

skills:  ## sync skills into ~/.claude/skills
	$(PY) $(SCRIPTS)/validate_skills.py
	$(PY) $(SCRIPTS)/sync_skills.py --fix --prune

brewfile:  ## regenerate the Brewfile from packages/manifest.json
	$(PY) $(SCRIPTS)/packages.py --emit brewfile

# Uses python3, not $(PY): the whole point is a machine with no venv yet.
verify:  ## bootstrap a clean Arch + Ubuntu in containers (slow, needs docker)
	bash $(SCRIPTS)/verify_linux.sh --skip-heavy --demo

## ----------------------------------------------------------------- demo ---

# One command that exercises the whole chain against the checked-in fixtures.
# If all six artefacts land in out/, the stack works end to end. Each step is
# independent, so a partial result still tells you which half is broken.
demo: demo-clean  ## end-to-end smoke test — six artefacts in out/
	@echo ""
	@echo "▸ 1/6  screenshot → framed, backdropped, captioned"
	$(PY) skills/shot/scripts/frame.py -i fixtures/raw.png -o $(OUT)/demo/shot.png \
	  --chrome browser --url "console.cloud.google.com" --backdrop hero \
	  --caption "Deploy in 90 seconds"
	@echo ""
	@echo "▸ 2/6  screenshot → numbered callouts from YAML"
	$(PY) skills/shot/scripts/annotate.py -i fixtures/raw.png -o $(OUT)/demo/annotated.png \
	  --spec fixtures/steps.yaml
	@echo ""
	@echo "▸ 3/6  recording → silence cut"
	$(PY) skills/vidkit/scripts/autocut.py -i fixtures/rec.mp4 -o $(OUT)/demo/cut.json \
	  --render $(OUT)/demo/cut.mp4
	@echo ""
	@echo "▸ 4/6  cut → NLE timeline for Resolve"
	$(PY) skills/vidkit/scripts/timeline.py -s $(OUT)/demo/cut.json -o $(OUT)/demo/edit.xml
	@echo ""
	@echo "▸ 5/6  outline → branded deck"
	$(PY) skills/deck/scripts/outline.py -i fixtures/outline.md -o $(OUT)/demo/deck.md \
	  --footer "creative-stack demo"
	@echo ""
	@echo "▸ 6/6  deck → PDF"
	$(PY) skills/deck/scripts/build.py -i $(OUT)/demo/deck.md -o $(OUT)/demo/deck.pdf
	@echo ""
	@$(MAKE) --no-print-directory demo-verify

demo-verify:
	@echo "======================================================================"
	@fail=0; for f in $(OUT)/demo/shot.png $(OUT)/demo/annotated.png \
	                 $(OUT)/demo/cut.mp4 $(OUT)/demo/edit.xml \
	                 $(OUT)/demo/deck.md $(OUT)/demo/deck.pdf; do \
	  if [ -s "$$f" ]; then \
	    printf '  \033[32m✓\033[0m %-28s %s\n' "$$(basename $$f)" "$$(du -h $$f | cut -f1)"; \
	  else \
	    printf '  \033[31m✗\033[0m %-28s missing or empty\n' "$$(basename $$f)"; fail=1; \
	  fi; \
	done; \
	echo "======================================================================"; \
	if [ $$fail -eq 0 ]; then echo "  🎬 all six artefacts present — the stack works"; \
	else echo "  run 'make doctor' to see which tool is missing"; exit 1; fi

demo-clean:
	@rm -rf $(OUT)/demo

## ---------------------------------------------------------------- clean ---

clean:  ## remove generated output
	rm -rf $(OUT) templates/deck/.generated
	find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +

distclean: clean  ## also remove the venv and the downloaded soundfont
	rm -rf .venv assets/soundfonts
