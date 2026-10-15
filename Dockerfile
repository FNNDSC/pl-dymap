# pl-dymap: Python and the package; the plugin runs no programs of its own.
#
#   docker build -t localhost/fnndsc/pl-dymap:2.1.1 .
#   docker run --rm localhost/fnndsc/pl-dymap:2.1.1 dymap --version

FROM docker.io/python:3.12-slim

LABEL org.opencontainers.image.authors="FNNDSC <dev@babyMRI.org>" \
      org.opencontainers.image.title="pl-dymap" \
      org.opencontainers.image.description="Map a ChRIS pipeline over the directories of an input: one workflow per item" \
      org.opencontainers.image.source="https://github.com/FNNDSC/pl-dymap" \
      org.opencontainers.image.version="2.1.1"

ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

WORKDIR /usr/local/src/pl-dymap
COPY . .
RUN pip install --no-cache-dir .

CMD ["dymap", "--man"]
