FROM postgres:17-alpine

COPY db-restore/copy/restore.sh /usr/local/bin/restore
COPY db-restore/copy/verify.sh /usr/local/bin/verify-restore
RUN chmod 0555 /usr/local/bin/restore /usr/local/bin/verify-restore
ENTRYPOINT ["/usr/local/bin/restore"]
