#!/bin/sh
# Hosts and `docker run -v` often mount the data volume owned by root. Hand /data to the greenlight
# user, then drop root: greenlight never runs as root, and can always write its database.
# Started as a non-root user already (some platforms do that), it just runs.
set -e
if [ "$(id -u)" = 0 ]; then
  mkdir -p /data
  chown -R greenlight:greenlight /data
  HOME=/home/greenlight exec setpriv --reuid=greenlight --regid=greenlight --init-groups greenlight "$@"
fi
exec greenlight "$@"
