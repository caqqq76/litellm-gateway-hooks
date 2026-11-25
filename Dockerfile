# Carrier image for the offline bundle: the litellm-gateway-hooks wheel, its wheelhouse and install.sh.
#
#   docker build -t litellm-gateway-hooks:<version> .
#
# Install into a LiteLLM proxy image:
#
#   COPY --from=litellm-gateway-hooks:<version> /opt/litellm-gateway-hooks /tmp/gateway-hooks
#   RUN /tmp/gateway-hooks/install.sh /path/to/proxy/python && rm -rf /tmp/gateway-hooks
#
# Or copy the bundle out to a host:
#
#   docker run --rm -v "$PWD/out:/out" litellm-gateway-hooks:<version> cp -r /opt/litellm-gateway-hooks /out/

FROM python:3.12-slim AS build
RUN pip install --no-cache-dir uv
WORKDIR /src
COPY . .
RUN scripts/build_bundle.sh && mv build/litellm-gateway-hooks-*-bundle /bundle

FROM busybox:1.37
COPY --from=build /bundle /opt/litellm-gateway-hooks
WORKDIR /opt/litellm-gateway-hooks
CMD ["ls", "-R", "/opt/litellm-gateway-hooks"]
