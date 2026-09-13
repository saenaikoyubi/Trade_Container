FROM postgres:17-alpine

COPY db-backup/copy/backup.sh /usr/local/bin/backup
RUN chmod 0555 /usr/local/bin/backup
ENTRYPOINT ["/usr/local/bin/backup"]

