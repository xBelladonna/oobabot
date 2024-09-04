ARG IMAGE=${IMAGE:-nvidia/cuda:12.2.2-runtime-ubuntu22.04}

# Builder stage to checkout code and copy requirements from
FROM alpine AS builder
RUN apk --no-cache add git
WORKDIR /app
COPY . .

# Main image
FROM $IMAGE

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt install -y --no-install-recommends \
        python3-pip tzdata espeak-ng libespeak-ng1 libclblast1 && \
    apt clean && rm -rf /var/lib/apt/lists/*
RUN pip --no-cache-dir install poetry

WORKDIR /app

# Check if dependencies have changed
COPY --from=builder /app/pyproject.toml /app/poetry.lock ./
RUN poetry install --no-interaction

COPY --from=builder /app/* ./
RUN poetry install --no-interaction
RUN rm -rf ~/.cache/pypoetry/{cache,artifacts}

CMD ["poetry", "run", "oobabot"]
