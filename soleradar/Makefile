# Everything you need, without remembering flags.
.PHONY: install dev build run test verify clean

install:
	python3 -m venv .venv
	./.venv/bin/pip install -e api[dev]
	npm --prefix web install

build:
	npm --prefix web run build

run: build
	PYTHONPATH=api ./.venv/bin/python -m soleradar

# Two processes: hot-reloading UI on :5173 proxying the API on :8787.
dev:
	PYTHONPATH=api ./.venv/bin/python -m uvicorn soleradar.main:app --reload --port 8787 & \
	npm --prefix web run dev

test:
	./.venv/bin/python -m pytest tests -q
	npm --prefix web run typecheck

verify: build
	./verify.sh

clean:
	rm -rf var web/dist
