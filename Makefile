.PHONY: install-pg prepare-benchmarks benchmark benchmark-tpcc benchmark-twitter benchmark-ycsb test
install-pg:
	./scripts/install_postgres_benchbase.sh

prepare-benchmarks:
	./scripts/run_benchmarks.sh prepare 10 2

benchmark:
	./scripts/run_benchmarks.sh run 30 2

benchmark-tpcc:
	python3 -m app.benchmark.cli tpcc --duration 30 --terminals 2 --reuse-data

benchmark-twitter:
	python3 -m app.benchmark.cli twitter --duration 30 --terminals 2 --reuse-data

benchmark-ycsb:
	python3 -m app.benchmark.cli ycsb --duration 30 --terminals 2 --reuse-data

test:
	python3 -m pytest -q
