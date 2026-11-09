# Ontwikkelcommando's (geen installatiemethode; installeren gaat als standaard TrueNAS Custom App).
PY ?= .venv/bin/python
API := apps/api
IMAGE ?= zoeken-en-persoonlijke-ai:dev

.PHONY: venv lint test e2e ui interop openapi lock image smoke clean

venv:
	python3.12 -m venv .venv && .venv/bin/pip install -q -r $(API)/requirements.lock && .venv/bin/pip install -q -e '$(API)[dev]'

lint:
	cd $(API) && ../../$(PY) -m ruff check . && ../../$(PY) -m ruff format --check . && ../../$(PY) -m mypy
	$(PY) -m ruff check tests/e2e tests/interop tests/ui deploy/allinone && $(PY) -m ruff format --check tests/e2e tests/interop tests/ui deploy/allinone

test:
	cd $(API) && ../../$(PY) -m pytest -q

e2e:
	$(PY) -m pytest -q tests/e2e -p no:cacheprovider

# Statuspagina in Peil: axe-core, overloop op 320 px, CSP en de schermafdrukken (360/768/1440 × licht/donker).
UI_OUT ?= build/statuspagina
SHOTS ?= docs/screenshots/peil/statuspagina
ui:
	cd tests/ui && npm ci --no-audit --no-fund
	$(PY) tests/ui/render_toestanden.py --out $(UI_OUT)
	nice -n 10 node tests/ui/statuspagina.mjs --in $(UI_OUT) --rapport $(SHOTS)/CONTROLE.json --schermafdrukken $(SHOTS)

# ALLEEN lokaal: dienst tegen de ECHTE Hub (ai-server-manager) in een eigen venv; HUB_PY = Python van die venv.
interop:
	SEARCH_AI_REAL_HUB_PYTHON=$(HUB_PY) $(PY) -m pytest -q tests/interop -p no:cacheprovider

openapi:
	$(PY) -m search_ai openapi --out docs/openapi/zoeken-en-persoonlijke-ai.openapi.json

# Runtime-lock opnieuw maken in een schone omgeving (na wijzigen van afhankelijkheden in pyproject.toml).
lock:
	rm -rf .lockvenv && python3.12 -m venv .lockvenv && .lockvenv/bin/pip install -q ./$(API)
	{ echo "# Runtime-afhankelijkheden (exact gepind) voor de alles-in-één image. Bijwerken: zie Makefile 'lock'."; \
	  .lockvenv/bin/pip freeze --exclude zoeken-en-persoonlijke-ai; } > $(API)/requirements.lock
	rm -rf .lockvenv $(API)/build

# Eén build tegelijk en met lage prioriteit (gedeelde bouwomgeving).
image:
	nice -n 10 docker buildx build --load -f deploy/allinone/Dockerfile -t $(IMAGE) .

smoke:
	deploy/allinone/smoke-test.sh $(IMAGE)

clean:
	find . -name __pycache__ -prune -exec rm -rf {} + ; rm -rf $(API)/build .lockvenv
	docker image rm $(IMAGE) 2>/dev/null || true
