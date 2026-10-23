PY ?= .venv/bin/python
PIP ?= .venv/bin/pip
PORT ?= 8787

.PHONY: help venv install test lint roles docs check smoke demo serve panel clean

help:  ## show this help
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv:  ## create .venv with a pinned Python
	python3 -m venv .venv
	$(PIP) install --upgrade pip

install: venv  ## venv + editable install + dev extras
	$(PIP) install -e ".[dev]"

test:  ## run the full offline suite
	$(PY) -m pytest -q

lint:  ## ruff check + roles validation
	-$(PY) -m ruff check nexus tests scripts
	$(PY) scripts/build_roles.py --check --stats

roles:  ## regenerate roles.yaml from the generator
	$(PY) scripts/build_roles.py

docs:  ## regenerate docs/roles.md
	$(PY) scripts/build_roles.py --docs

check:  ## CI parity: roles in sync + tests + CLI smoke
	$(PY) scripts/build_roles.py --check
	$(PY) scripts/build_roles.py --out /tmp/roles-check.yaml
	diff -u roles.yaml /tmp/roles-check.yaml
	$(PY) -m pytest -q

smoke:  ## CLI smoke test (no keys required)
	$(PY) -m nexus version
	$(PY) -m nexus roles
	$(PY) -m nexus agents
	$(PY) -m nexus pipeline --list
	-$(PY) -m nexus doctor

demo:  ## scripted offline run through the whole loop
	$(PY) -m nexus task 'add a health-check endpoint to the API' --demo --yes

serve:  ## local HTTP + SSE server (VS Code sidebar backend)
	$(PY) -m nexus serve --host 0.0.0.0 --port $(PORT)

panel:  ## open the browser panel
	$(PY) -m nexus panel --port $(PORT)

clean:  ## remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache dist build *.egg-info
	find . -type d -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} +
