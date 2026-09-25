PYTHON ?= python3
PACKAGE_VERSION := $(shell awk -F '"' '/^version = / { print $$2; exit }' pyproject.toml)
WHEEL_PATH := dist/debugbundle_python-$(PACKAGE_VERSION)-py3-none-any.whl
PYTHON_IMAGE ?= python:3.12-bookworm
DOCKER_RUN = docker run --rm -v "$(CURDIR):/workspace" -w /workspace $(PYTHON_IMAGE)

.PHONY: install-docker test-focused check-docker
install-docker:
	$(DOCKER_RUN) sh -c 'python -m venv .venv-docker && .venv-docker/bin/pip install -e ".[dev]"'

test-focused:
	$(DOCKER_RUN) .venv-docker/bin/pytest $(TEST_FILES)

check-docker:
	$(DOCKER_RUN) sh -c '.venv-docker/bin/ruff check . && .venv-docker/bin/mypy src && .venv-docker/bin/pytest --cov=src/debugbundle --cov-report=term-missing --cov-report=json:coverage.json -q && .venv-docker/bin/python scripts/check_coverage.py coverage.json && .venv-docker/bin/python -m build'

.PHONY: smoke

smoke:
	$(PYTHON) -m build
	$(PYTHON) smoke/run_app_driven_smoke.py --wheel "$(WHEEL_PATH)"

.PHONY: smoke-docker
smoke-docker:
	$(DOCKER_RUN) python smoke/run_app_driven_smoke.py --wheel "$(WHEEL_PATH)"
