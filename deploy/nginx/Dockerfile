FROM nginx:1.27-alpine

RUN apk add --no-cache openssl

ARG NGINX_TEMPLATE=deploy/nginx/default.conf.template
COPY ${NGINX_TEMPLATE} /etc/nginx/templates/default.conf.template
COPY deploy/nginx/10-self-signed-certificate.sh /docker-entrypoint.d/10-self-signed-certificate.sh

RUN chmod 0555 /docker-entrypoint.d/10-self-signed-certificate.sh
