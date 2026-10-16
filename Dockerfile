# syntax=docker/dockerfile:1
#
# PokeMaker API image. Published by CI to ghcr.io/rchacon/pokemaker-api (arm64)
# and run on EC2 behind CloudFront; `docker compose --profile api up` runs the
# same image locally.
#
# Nothing secret is baked in: settings come from the environment, and secrets
# (OpenAI key, PIN token secret, CloudFront origin secret) are read from SSM
# Parameter Store at startup using the instance's IAM role.

FROM python:3.12-slim-trixie AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install ".[server]"

FROM python:3.12-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY --from=build /install /usr/local
RUN useradd --system --uid 10001 --no-create-home pokemaker
USER pokemaker
# An empty working directory: there is never a .env file inside the image.
WORKDIR /app
EXPOSE 8000
# Plain HTTP by default (local use). The server overrides the command to add
# TLS on port 8443; see pokemaker-infra.
CMD ["uvicorn", "pokemaker.main:app", "--host", "0.0.0.0", "--port", "8000"]
