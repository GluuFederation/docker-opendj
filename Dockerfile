FROM adoptopenjdk/openjdk11:jre-11.0.8_10-alpine

# symlink JVM
RUN mkdir -p /usr/lib/jvm/default-jvm /usr/java/latest \
    && ln -sf /opt/java/openjdk /usr/lib/jvm/default-jvm/jre \
    && ln -sf /usr/lib/jvm/default-jvm/jre /usr/java/latest/jre

# ===============
# Alpine packages
# ===============

RUN apk update \
    && apk add --no-cache openssl py3-pip tini curl \
    && apk add --no-cache --virtual build-deps wget git

# ======
# OpenDJ
# ======

ENV GLUU_VERSION=4.0.0.gluu
ENV GLUU_BUILD_DATE="2020-09-23 09:18"

RUN wget -q https://ox.gluu.org/maven/org/gluufederation/opendj/opendj-server-legacy/${GLUU_VERSION}/opendj-server-legacy-${GLUU_VERSION}.zip -P /tmp \
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

RUN apk add --no-cache py3-cryptography
COPY requirements.txt /app/requirements.txt
RUN pip3 install -U pip \
    && pip3 install -r /app/requirements.txt --no-cache-dir \
    && rm -rf /src/pygluu-containerlib/.git

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
EXPOSE 8989
EXPOSE 4444

# ==========
# Config ENV
# ==========

ENV GLUU_CONFIG_ADAPTER=consul \
    GLUU_CONFIG_CONSUL_HOST=localhost \
    GLUU_CONFIG_CONSUL_PORT=8500 \
    GLUU_CONFIG_CONSUL_CONSISTENCY=stale \
    GLUU_CONFIG_CONSUL_SCHEME=http \
    GLUU_CONFIG_CONSUL_VERIFY=false \
    GLUU_CONFIG_CONSUL_CACERT_FILE=/etc/certs/consul_ca.crt \
    GLUU_CONFIG_CONSUL_CERT_FILE=/etc/certs/consul_client.crt \
    GLUU_CONFIG_CONSUL_KEY_FILE=/etc/certs/consul_client.key \
    GLUU_CONFIG_CONSUL_TOKEN_FILE=/etc/certs/consul_token \
    GLUU_CONFIG_KUBERNETES_NAMESPACE=default \
    GLUU_CONFIG_KUBERNETES_CONFIGMAP=gluu \
    GLUU_CONFIG_KUBERNETES_USE_KUBE_CONFIG=false

# ==========
# Secret ENV
# ==========

ENV GLUU_SECRET_ADAPTER=vault \
    GLUU_SECRET_VAULT_SCHEME=http \
    GLUU_SECRET_VAULT_HOST=localhost \
    GLUU_SECRET_VAULT_PORT=8200 \
    GLUU_SECRET_VAULT_VERIFY=false \
    GLUU_SECRET_VAULT_ROLE_ID_FILE=/etc/certs/vault_role_id \
    GLUU_SECRET_VAULT_SECRET_ID_FILE=/etc/certs/vault_secret_id \
    GLUU_SECRET_VAULT_CERT_FILE=/etc/certs/vault_client.crt \
    GLUU_SECRET_VAULT_KEY_FILE=/etc/certs/vault_client.key \
    GLUU_SECRET_VAULT_CACERT_FILE=/etc/certs/vault_ca.crt \
    GLUU_SECRET_KUBERNETES_NAMESPACE=default \
    GLUU_SECRET_KUBERNETES_SECRET=gluu \
    GLUU_SECRET_KUBERNETES_USE_KUBE_CONFIG=false

# ===============
# Persistence ENV
# ===============

ENV GLUU_PERSISTENCE_TYPE=ldap \
    GLUU_PERSISTENCE_LDAP_MAPPING=default

# ===========
# Generic ENV
# ===========

ENV GLUU_LDAP_AUTO_REPLICATE=true \
    GLUU_ADMIN_PORT=4444 \
    GLUU_REPLICATION_PORT=8989 \
    GLUU_WAIT_MAX_TIME=300 \
    GLUU_WAIT_SLEEP_DURATION=10 \
    GLUU_MAX_RAM_PERCENTAGE=75.0 \
    GLUU_JAVA_OPTIONS="" \
    GLUU_LDAP_AUTO_REPLICATE=true \
    GLUU_LDAP_REPL_CHECK_INTERVAL=10 \
    GLUU_LDAP_REPL_MAX_RETRIES=30 \
    GLUU_SERF_PROFILE=lan \
    GLUU_SERF_LOG_LEVEL=warn

# ====
# misc
# =====

LABEL name="OpenDJ" \
    maintainer="Gluu Inc. <support@gluu.org>" \
    vendor="Gluu Federation" \
    version="4.2.1" \
    release="dev" \
    summary="Gluu OpenDJ" \
    description="Community fork of OpenDJ, an LDAP server originally developed by ForgeRock"

RUN mkdir -p /etc/certs /flag /deploy /app/tmp /etc/gluu/conf
COPY schemas/*.ldif /opt/opendj/template/config/schema/
COPY templates /app/templates
COPY scripts /app/scripts
RUN chmod +x /app/scripts/entrypoint.sh

ENTRYPOINT ["tini", "-e", "143" ,"-g", "--"]
CMD ["sh", "/app/scripts/entrypoint.sh"]
