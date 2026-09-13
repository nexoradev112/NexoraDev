FROM redis:7.4-alpine

COPY deploy/redis-entrypoint.sh /usr/local/bin/nexora-redis-entrypoint
RUN chmod 0555 /usr/local/bin/nexora-redis-entrypoint

USER 999:999
ENTRYPOINT ["nexora-redis-entrypoint"]
