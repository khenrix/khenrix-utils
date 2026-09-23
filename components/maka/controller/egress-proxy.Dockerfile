FROM python:3.12-slim@sha256:2fe5997d249a808b8eeea52c58a1dbffbba28754dc11699ef5c029f2d818ce79

COPY egress-proxy.lock /tmp/egress-proxy.lock
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/egress-proxy.lock \
    && python -c 'import importlib.metadata as m; assert m.version("mitmproxy") == "12.2.3"' \
    && rm /tmp/egress-proxy.lock

COPY egress_filter.py /opt/maka-eval/egress_filter.py
COPY entrypoint.sh /opt/maka-eval/entrypoint.sh
COPY keyed_mitmdump.py /opt/maka-eval/keyed_mitmdump.py
COPY test_egress_injector.py /opt/maka-eval/test_egress_injector.py
COPY test_egress_integration.py /opt/maka-eval/test_egress_integration.py
COPY test_egress_undici_h2.py /opt/maka-eval/test_egress_undici_h2.py
COPY test_egress_undici_h2_client.mjs /opt/maka-eval/test_egress_undici_h2_client.mjs

LABEL io.maka.lab.source-revision="6cb8c58084d043f9b87421807fbee1d1ad3bdc03" \
      io.maka.lab.python-base="python:3.12-slim@sha256:2fe5997d249a808b8eeea52c58a1dbffbba28754dc11699ef5c029f2d818ce79"

ENTRYPOINT ["/opt/maka-eval/entrypoint.sh"]
