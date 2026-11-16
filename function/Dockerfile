# syntax=docker/dockerfile:1
# The workload function: SeBS 501.graph-pagerank (Python), unmodified, behind our HTTP wrapper.
FROM python:3.12-slim

RUN pip install --no-cache-dir igraph==0.11.4

WORKDIR /app
# SeBS commit 089e5c6ccece0b4d9508e08eee80cf3527590c4f (2026-10-24). The checksum pins the file content.
ADD --checksum=sha256:e0944f64d22a806f3bbd8d8c4f96c521b3438fd60ed7c097ade66ed7794f7c33 \
    https://raw.githubusercontent.com/spcl/serverless-benchmarks/089e5c6ccece0b4d9508e08eee80cf3527590c4f/benchmarks/500.scientific/501.graph-pagerank/python/function.py \
    /app/function.py
COPY server.py sampler.py /app/

ENV PYTHONUNBUFFERED=1
EXPOSE 8080 8081
CMD ["python", "/app/server.py"]
