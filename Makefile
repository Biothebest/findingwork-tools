PYTHON ?= python3
SCRAPER := .agents/skills/scrape-jobs/scripts/run.py
TESTS := .agents/skills/scrape-jobs/tests

.DEFAULT_GOAL := help
.PHONY: help check preflight test

help:
	@printf '%s\n' \
	  'make check                 Run integrity checks and offline regression tests.' \
	  'make preflight             Verify both reviewed skill manifests.' \
	  'make test                  Run the existing suite after preflight.' \
	  'make check PYTHON=/path/to/python3  Choose a Python 3.9+ interpreter.' \
	  '' \
	  'No maintenance target collects jobs, changes launchd, or resets daily state.' \
	  'Private inputs and job-search-daily/ are deliberately not in Git.' \
	  'Restore them from a private backup before operating a new checkout.' \
	  'Setup, backup, and update rules: .agents/skills/scrape-jobs/SKILL.md'

check: test

preflight:
	$(PYTHON) -B $(SCRAPER) preflight

test: preflight
	$(PYTHON) -B -m unittest discover -s $(TESTS) -p 'test_*.py' -v
