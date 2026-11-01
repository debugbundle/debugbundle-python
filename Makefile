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

.PHONY: format-transport-tests
format-transport-tests:
	$(DOCKER_RUN) .venv-docker/bin/ruff format tests/test_transport_reliability.py

.PHONY: format-focused
format-focused:
	$(DOCKER_RUN) sh -c '.venv-docker/bin/ruff check --select I --fix $(FORMAT_FILES) && .venv-docker/bin/ruff format $(FORMAT_FILES)'

check-docker:
	$(DOCKER_RUN) sh -c '.venv-docker/bin/ruff check . && .venv-docker/bin/mypy src && .venv-docker/bin/pytest --cov=src/debugbundle --cov-report=term-missing --cov-report=json:coverage.json -q && .venv-docker/bin/python scripts/check_coverage.py coverage.json && .venv-docker/bin/python -m build'

.PHONY: smoke

smoke:
	$(PYTHON) -m build
	$(PYTHON) smoke/run_app_driven_smoke.py --wheel "$(WHEEL_PATH)"

.PHONY: smoke-docker
smoke-docker:
	$(DOCKER_RUN) python smoke/run_app_driven_smoke.py --wheel "$(WHEEL_PATH)"

.PHONY: native-runtime-docker
native-runtime-docker:
	docker run --rm --mount "type=bind,source=$(CURDIR),target=/workspace,readonly" -w /workspace $(PYTHON_IMAGE) sh -c 'python --version && python smoke/run_app_driven_smoke.py --wheel "$(WHEEL_PATH)"'

# Isolate runtime-specific coverage from the default development environment.
.PHONY: runtime-coverage-docker
runtime-coverage-docker:
	$(DOCKER_RUN) sh -c 'python -m venv /tmp/debugbundle-coverage && /tmp/debugbundle-coverage/bin/pip install -e ".[dev]" >/dev/null && mkdir -p .tmp && /tmp/debugbundle-coverage/bin/pytest --cov=src/debugbundle --cov-report=json:.tmp/runtime-coverage.json -q && /tmp/debugbundle-coverage/bin/python scripts/check_coverage.py .tmp/runtime-coverage.json'

.PHONY: evidence-performance
evidence-performance:
	docker run --rm --network none -v "$(CURDIR):/workspace:ro" -w /workspace -e PYTHONPATH=/workspace/src $(PYTHON_IMAGE) .venv-docker/bin/python perf/evidence_capture.py
