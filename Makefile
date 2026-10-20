PYTHON ?= python

# Prefer the existing project-local Windows environment when present. On Linux
# or macOS, activate the project venv or pass PYTHON=/path/to/python.
ifeq ($(OS),Windows_NT)
ifneq (,$(wildcard .venv-api/Scripts/python.exe))
PYTHON := .venv-api/Scripts/python.exe
endif
endif

.PHONY: test

test:
	"$(PYTHON)" -m unittest discover -s . -p "test_*.py"
