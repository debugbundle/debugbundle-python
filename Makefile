PYTHON ?= python3
PACKAGE_VERSION := $(shell awk -F '"' '/^version = / { print $$2; exit }' pyproject.toml)
WHEEL_PATH := dist/debugbundle_python-$(PACKAGE_VERSION)-py3-none-any.whl

.PHONY: smoke

smoke:
	$(PYTHON) -m build
	$(PYTHON) smoke/run_app_driven_smoke.py --wheel "$(WHEEL_PATH)"