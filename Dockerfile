FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 WORKDESK_DATA_DIR=/var/lib/workdesk
WORKDIR /opt/workdesk
COPY requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements-cloud.txt \
    && useradd --uid 10001 --create-home workdesk \
    && mkdir -p /var/lib/workdesk && chown workdesk:workdesk /var/lib/workdesk
COPY app.py notifications.py contributions.py sync_diagnostics.py local_backups.py phone_access.py view_filters.py cloud.py cloud_identity.py cloud_push.py container_start.py ./
COPY static ./static
EXPOSE 10000
# A new mounted disk may be root-owned. The launcher prepares only that directory,
# then switches to UID/GID 10001 before starting the HTTP server.
CMD ["python", "container_start.py"]
