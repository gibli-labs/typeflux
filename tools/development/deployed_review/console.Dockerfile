FROM nginx:stable-alpine@sha256:e9b2d7624fd7957f4ed44ae0a272d93fb73aeb8512ba8916f8118ae6143fd115
COPY artifacts/console-dist.tar.gz /tmp/console.tar.gz
RUN rm -rf /usr/share/nginx/html/* && tar -xzf /tmp/console.tar.gz -C /usr/share/nginx/html \
    && rm /tmp/console.tar.gz
COPY console.conf /etc/nginx/conf.d/default.conf
