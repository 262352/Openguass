.PHONY: bootstrap check-env install-pg prepare-benchmarks benchmark benchmark-tpcc benchmark-twitter benchmark-ycsb test
PYTHON ?= .venv/bin/python

bootstrap:
	./scripts/bootstrap_test_environment.sh

check-env:
	./scripts/bootstrap_test_environment.sh --check-only
install-pg:
	./scripts/install_postgres_benchbase.sh

prepare-benchmarks:
	./scripts/run_benchmarks.sh prepare 10 2

benchmark:
	./scripts/run_benchmarks.sh run 30 2

benchmark-tpcc:
	$(PYTHON) -m app.benchmark.cli tpcc --duration 30 --terminals 2 --reuse-data

benchmark-twitter:
	$(PYTHON) -m app.benchmark.cli twitter --duration 30 --terminals 2 --reuse-data

benchmark-ycsb:
	$(PYTHON) -m app.benchmark.cli ycsb --duration 30 --terminals 2 --reuse-data

test:
	$(PYTHON) -m pytest -q
