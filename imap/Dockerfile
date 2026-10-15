FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends dovecot-imapd \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 5000 vmail \
 && useradd -u 5000 -g vmail -d /srv/mail -s /usr/sbin/nologin vmail \
 && mkdir -p /srv/mail && chown vmail:vmail /srv/mail

COPY dovecot.conf /etc/dovecot/dovecot.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 143
VOLUME /srv/mail
ENTRYPOINT ["/entrypoint.sh"]
CMD ["dovecot", "-F"]
