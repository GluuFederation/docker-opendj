FROM alpine:3.13

# ===============
# Alpine packages
# ===============

RUN apk update \
    && apk add --no-cache openssl py3-pip tini curl openjdk11-jre-headless py3-cryptography \
    && apk add --no-cache --virtual build-deps wget git \
    && mkdir -p /usr/java/latest \
    && ln -sf /usr/lib/jvm/default-jvm/jre /usr/java/latest/jre

# ======
# OpenDJ
# ======

ENV GLUU_VERSION=4.0.0.gluu
ENV GLUU_BUILD_DATE="2020-09-23 09:18"
ENV GLUU_SOURCE_URL=https://ox.gluu.org/maven/org/gluufederation/opendj/opendj-server-legacy/${GLUU_VERSION}/opendj-server-legacy-${GLUU_VERSION}.zip

RUN wget -q ${GLUU_SOURCE_URL} -P /tmp \
    && mkdir -p /opt \
    && unzip -qq /tmp/opendj-server-legacy-${GLUU_VERSION}.zip -d /opt \
    && rm -f /tmp/opendj-server-legacy-${GLUU_VERSION}.zip

# ====
# Serf
# ====

ARG SERF_VERSION=0.8.2
RUN wget -q https://releases.hashicorp.com/serf/${SERF_VERSION}/serf_${SERF_VERSION}_linux_amd64.zip -O /tmp/serf.zip \
    && unzip -qq /tmp/serf.zip -d /tmp \
    && cp /tmp/serf /usr/bin/serf \
    && chmod +x /usr/bin/serf \
    && rm -f /tmp/serf*

# ======
# Python
# ======

COPY requirements.txt /app/requirements.txt
RUN pip3 install -U pip \
    && pip3 install -r /app/requirements.txt --no-cache-dir \
    && rm -rf /src/jans-pycloudlib/.git

# =======
# cleanup
# =======

RUN apk del build-deps \
    && rm -rf /var/cache/apk/*

# =======
# License
# =======

RUN mkdir -p /licenses
COPY LICENSE /licenses/

# ====
# misc
# ====

EXPOSE 1636

# ==========
# Config ENV
# ==========

ENV CN_CONFIG_ADAPTER=consul \
    CN_CONFIG_CONSUL_HOST=localhost \
    CN_CONFIG_CONSUL_PORT=8500 \
    CN_CONFIG_CONSUL_CONSISTENCY=stale \
    CN_CONFIG_CONSUL_SCHEME=http \
    CN_CONFIG_CONSUL_VERIFY=false \
    CN_CONFIG_CONSUL_CACERT_FILE=/etc/certs/consul_ca.crt \
    CN_CONFIG_CONSUL_CERT_FILE=/etc/certs/consul_client.crt \
    CN_CONFIG_CONSUL_KEY_FILE=/etc/certs/consul_client.key \
    CN_CONFIG_CONSUL_TOKEN_FILE=/etc/certs/consul_token \
    CN_CONFIG_CONSUL_NAMESPACE=jans \
    CN_CONFIG_KUBERNETES_NAMESPACE=default \
    CN_CONFIG_KUBERNETES_CONFIGMAP=gluu \
    CN_CONFIG_KUBERNETES_USE_KUBE_CONFIG=false

# ==========
# Secret ENV
# ==========

ENV CN_SECRET_ADAPTER=vault \
    CN_SECRET_VAULT_SCHEME=http \
    CN_SECRET_VAULT_HOST=localhost \
    CN_SECRET_VAULT_PORT=8200 \
    CN_SECRET_VAULT_VERIFY=false \
    CN_SECRET_VAULT_ROLE_ID_FILE=/etc/certs/vault_role_id \
    CN_SECRET_VAULT_SECRET_ID_FILE=/etc/certs/vault_secret_id \
    CN_SECRET_VAULT_CERT_FILE=/etc/certs/vault_client.crt \
    CN_SECRET_VAULT_KEY_FILE=/etc/certs/vault_client.key \
    CN_SECRET_VAULT_CACERT_FILE=/etc/certs/vault_ca.crt \
    CN_SECRET_VAULT_NAMESPACE=jans \
    CN_SECRET_KUBERNETES_NAMESPACE=default \
    CN_SECRET_KUBERNETES_SECRET=gluu \
    CN_SECRET_KUBERNETES_USE_KUBE_CONFIG=false

# ===========
# Generic ENV
# ===========

ENV CN_LDAP_AUTO_REPLICATE=true \
    CN_LDAP_ADVERTISE_ADMIN_PORT=4444 \
    CN_LDAP_ADVERTISE_REPLICATION_PORT=8989 \
    CN_LDAP_ADVERTISE_LDAPS_PORT=1636 \
    CN_LDAP_REPL_CHECK_INTERVAL=10 \
    CN_LDAP_REPL_MAX_RETRIES=30 \
    CN_ADMIN_PORT=4444 \
    CN_REPLICATION_PORT=8989 \
    CN_WAIT_MAX_TIME=300 \
    CN_WAIT_SLEEP_DURATION=10 \
    CN_MAX_RAM_PERCENTAGE=75.0 \
    CN_JAVA_OPTIONS="" \
    CN_SERF_PROFILE=lan \
    CN_SERF_LOG_LEVEL=warn \
    CN_SERF_ADVERTISE_ADDR="" \
    CN_SERF_KEY_FILE=/etc/jans/conf/serf-key \
    CN_PERSISTENCE_TYPE=ldap \
    CN_PERSISTENCE_LDAP_MAPPING=default

# ====
# misc
# =====

LABEL name="OpenDJ" \
    maintainer="Gluu Inc. <support@gluu.org>" \
    vendor="Gluu Federation" \
    version="5.0.0" \
    release="a1" \
    summary="Gluu OpenDJ" \
    description="Community fork of OpenDJ, an LDAP server originally developed by ForgeRock"

RUN mkdir -p /etc/certs /deploy /etc/jans/conf
COPY schemas/*.ldif /opt/opendj/template/config/schema/
COPY templates /app/templates
COPY scripts /app/scripts
RUN chmod +x /app/scripts/entrypoint.sh

ENTRYPOINT ["tini", "-e", "143" ,"-g", "--"]
CMD ["sh", "/app/scripts/entrypoint.sh"]
