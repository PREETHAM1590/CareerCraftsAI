FROM alpine:3.23
RUN apk add --no-cache squid
COPY squid.conf /etc/squid/squid.conf
# Production drops the private-HTTP exception for the synthetic test portal.
ARG ALLOW_TEST_PORTAL=0
RUN if [ "$ALLOW_TEST_PORTAL" != 1 ]; then sed -i '/fixture/d' /etc/squid/squid.conf; fi
CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]
