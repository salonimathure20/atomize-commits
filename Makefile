.PHONY: test serve docker-build docker-run clean

test:            ## run every test
	cd engine && python -m pytest tests -q
	python -m pytest service/tests -q
	python -m pytest ci/tests -q
	cd extension && npm test

hygiene:         ## run the PR check against a locally running service
	python3 ci/hygiene.py --service http://localhost:8000 \
		--base $(BASE) --head HEAD --dry-run

serve:           ## run the service locally with auto-reload
	cd service && PYTHONPATH=../engine RELOAD=1 python app.py

docs:            ## open the generated API documentation
	@echo 'Start the service, then visit http://localhost:8000/docs'

docker-build:    ## build the image
	docker build -t commit-splitter:local .

docker-run:      ## build and run via compose
	docker compose up --build

docker-shell:    ## poke around inside the image
	docker run --rm -it --entrypoint /bin/bash commit-splitter:local

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete
