FROM alpine:3.23
RUN apk add --no-cache squid
COPY squid.conf /etc/squid/squid.conf
# Production drops the private-HTTP exception for the synthetic test portal.
ARG ALLOW_TEST_PORTAL=0
RUN if [ "$ALLOW_TEST_PORTAL" != 1 ]; then sed -i '/fixture/d' /etc/squid/squid.conf; fi
# Squid opens its access log after dropping to the squid user, and Docker's
# stdout pipe belongs to root: hand the pipe over first, then become Squid.
CMD ["sh", "-c", "chown squid /proc/self/fd/1 && exec squid -N -f /etc/squid/squid.conf"]
