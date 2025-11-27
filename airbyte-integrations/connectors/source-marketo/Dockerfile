FROM python:3.9.11-slim as base

# build and load all requirements
FROM base as builder
WORKDIR /airbyte/integration_code

# upgrade pip to the latest version
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        tzdata \
    && pip install --upgrade pip \
    && rm -rf /var/lib/apt/lists/*

COPY setup.py ./
# install necessary packages to a temporary folder
RUN pip install --prefix=/install .

# build a clean environment
FROM base
WORKDIR /airbyte/integration_code

# copy all loaded and built libraries to a pure basic image
COPY --from=builder /install /usr/local
# add default timezone settings
RUN ln -snf /usr/share/zoneinfo/Etc/UTC /etc/localtime \
    && echo "Etc/UTC" > /etc/timezone

# bash is installed for more convenient debugging.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
    && rm -rf /var/lib/apt/lists/*

# copy payload code only
COPY main.py ./
COPY source_marketo ./source_marketo

ENV AIRBYTE_ENTRYPOINT "python /airbyte/integration_code/main.py"
ENTRYPOINT ["python", "/airbyte/integration_code/main.py"]

LABEL io.airbyte.version=1.1.0
LABEL io.airbyte.name=airbyte/source-marketo
