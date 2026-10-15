#!/bin/sh
# Erzeugt die Benutzerdatei aus IMAP_USERS="benutzer:passwort,benutzer2:passwort2"
set -eu
: "${IMAP_USERS:?IMAP_USERS muss gesetzt sein (benutzer:passwort,...)}"

: > /etc/dovecot/users
OLDIFS=$IFS; IFS=','
for entry in $IMAP_USERS; do
  user=${entry%%:*}
  pass=${entry#*:}
  [ -n "$user" ] && [ "$user" != "$entry" ] || { echo "Ungültiger Eintrag in IMAP_USERS: $entry" >&2; exit 1; }
  echo "${user}:{PLAIN}${pass}::::::" >> /etc/dovecot/users
done
IFS=$OLDIFS
chown root:dovecot /etc/dovecot/users
chmod 640 /etc/dovecot/users
chown vmail:vmail /srv/mail

exec "$@"
