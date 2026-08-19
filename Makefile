.PHONY: test serve docker-build docker-run clean

test:            ## run every test
	cd engine && python -m pytest tests -q
	python -m pytest service/tests -q

serve:           ## run the service locally, no container
	cd service && PYTHONPATH=../engine python app.py

docker-build:    ## build the image
	docker build -t commit-splitter:local .

docker-run:      ## build and run via compose
	docker compose up --build

docker-shell:    ## poke around inside the image
	docker run --rm -it --entrypoint /bin/bash commit-splitter:local

clean:
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete
