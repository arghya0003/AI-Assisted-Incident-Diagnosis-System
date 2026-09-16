# Build context is the repository root, not this directory: the runner scores
# the anomaly detector by replaying recorded metrics through it, so it needs
# that service's modules in the image alongside its own.
FROM python:3.11-slim

WORKDIR /app

COPY services/evaluation-runner/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/anomaly-detector/detectors.py \
     services/anomaly-detector/grouping.py \
     services/anomaly-detector/deploy_window.py \
     services/anomaly-detector/staleness.py \
     /app/detector/

COPY services/evaluation-runner/ /app/

ENV DETECTOR_PACKAGE_PATH=/app/detector

# No CMD: this is a one-shot tool, always run with an explicit subcommand
# (`docker compose run --rm evaluation-runner live`), never left running.
ENTRYPOINT ["python", "main.py"]
