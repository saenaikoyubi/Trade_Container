FROM postgres:17-alpine

COPY postgres/copy/postgresql.conf /etc/postgresql/postgresql.conf
RUN mkdir -p /var/log/postgresql && chown -R postgres:postgres /var/log/postgresql
CMD ["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"]

