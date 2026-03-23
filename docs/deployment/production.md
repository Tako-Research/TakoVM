# Production Deployment

Guide for deploying Tako VM in production environments.

## Production Configuration

Create a production config file:

```yaml
# /etc/tako-vm/config.yaml

production_mode: true      # Require pre-built images

# Scale workers based on CPU cores
max_workers: 8
max_queue_size: 500

# Tighter limits
default_timeout: 30
max_timeout: 120
max_stdout_bytes: 32768    # 32KB
max_artifact_bytes: 5242880  # 5MB

# Security
enable_seccomp: true
```

## Pre-build Images

In production mode, images must be pre-built:

```bash
# Build images via the REST API (requires a running server)
curl -X POST http://localhost:8000/job-types/data-processing/build

# Verify images exist
docker images | grep tako-vm
```

!!! note "CLI support for building images is planned — see [GitHub #30](https://github.com/las7/TakoVM/issues/30)."

## Running with Systemd

Create a systemd service:

```ini
# /etc/systemd/system/tako-vm.service

[Unit]
Description=Tako VM Code Execution Service
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=tako-vm
Group=tako-vm
WorkingDirectory=/opt/tako-vm
Environment=TAKO_VM_CONFIG=/etc/tako-vm/config.yaml
ExecStart=/usr/local/bin/tako-vm server --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/var/lib/tako-vm

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl enable tako-vm
sudo systemctl start tako-vm
sudo systemctl status tako-vm
```

## Running with Docker Compose

The repository includes a production-ready `docker-compose.yaml`:

```bash
# Build and start
docker-compose --profile build up -d --build

# View logs
docker-compose logs -f tako-vm

# Stop
docker-compose down
```

To customize, mount your config file:

```yaml
services:
  tako-vm:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./tako_vm.yaml:/app/tako_vm.yaml:ro  # Add this line
```

!!! warning
    Mounting the Docker socket gives Tako VM access to the Docker daemon. In high-security environments, consider using Docker-in-Docker or a separate Docker host.

## Reverse Proxy (Nginx)

```nginx
# /etc/nginx/sites-available/tako-vm

upstream tako_vm {
    server 127.0.0.1:8000;
}

server {
    listen 443 ssl http2;
    server_name tako-vm.example.com;

    ssl_certificate /etc/ssl/certs/tako-vm.crt;
    ssl_certificate_key /etc/ssl/private/tako-vm.key;

    # Security headers
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;

    # Request size limits
    client_max_body_size 2M;

    location / {
        proxy_pass http://tako_vm;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Pass correlation ID through
        proxy_set_header X-Correlation-ID $http_x_correlation_id;

        # Timeouts for long-running jobs
        proxy_read_timeout 300s;
        proxy_connect_timeout 10s;
    }

    # Health check endpoint (no auth)
    location /health {
        proxy_pass http://tako_vm/health;
    }
}
```

## Built-in Resilience Features

Tako VM includes several built-in features for production reliability:

### Startup Cleanup

On startup, Tako VM automatically:
- Checks Docker daemon health
- Removes orphaned containers from previous runs
- Initializes the circuit breaker

### Circuit Breaker

The circuit breaker prevents cascading failures when Docker is unavailable:

- **Failure threshold**: 5 consecutive failures opens the circuit
- **Recovery timeout**: 30 seconds before testing recovery
- **Success threshold**: 2 successes closes the circuit

Monitor via health endpoint:
```bash
curl http://localhost:8000/health | jq '.circuit_breaker'
```

### Automatic Retry

Transient Docker failures are automatically retried:
- Max attempts: 2
- Exponential backoff with jitter

### Dead Letter Queue

Failed jobs are stored in a dead letter queue for investigation:
```bash
# Check DLQ stats
curl http://localhost:8000/dlq/stats

# List failed jobs
curl http://localhost:8000/dlq
```

### Correlation IDs

All requests include correlation IDs for distributed tracing:
- Auto-generated if not provided
- Included in logs and DLQ entries
- Returned in response headers

## Monitoring

### Health Checks

```bash
# Kubernetes/Docker health check
curl -f http://localhost:8000/health || exit 1
```

The health endpoint returns detailed status:
```json
{
  "status": "healthy",
  "docker_available": true,
  "circuit_breaker": {
    "state": "closed",
    "failure_count": 0
  },
  "queue_stats": {
    "pending": 0,
    "running": 2
  }
}
```

### Metrics to Monitor

| Metric | Source | Alert Threshold |
|--------|--------|-----------------|
| Queue depth | `/pool/stats` pending | > 50 |
| Worker utilization | running / max_workers | > 80% |
| Circuit breaker state | `/health` circuit_breaker.state | != closed |
| DLQ size | `/dlq/stats` total | > 10 |
| Error rate | Execution records | > 5% |
| P95 latency | Execution duration_ms | > 10s |

### Alerting on Circuit Breaker

```bash
#!/bin/bash
# /opt/tako-vm/check-health.sh

HEALTH=$(curl -s http://localhost:8000/health)
CB_STATE=$(echo $HEALTH | jq -r '.circuit_breaker.state')
DLQ_TOTAL=$(curl -s http://localhost:8000/dlq/stats | jq '.total')

if [ "$CB_STATE" = "open" ]; then
    echo "CRITICAL: Circuit breaker is OPEN"
    exit 2
fi

if [ "$DLQ_TOTAL" -gt 10 ]; then
    echo "WARNING: DLQ has $DLQ_TOTAL entries"
    exit 1
fi

echo "OK: System healthy"
exit 0
```

### Log Aggregation

Tako VM logs include correlation IDs for tracing:

```
2024-01-15 10:30:00 [abc-123-def] INFO tako_vm.server.queue: Worker 0 executing job 550e8400...
2024-01-15 10:30:01 [abc-123-def] INFO tako_vm.execution.worker: Job completed successfully
```

Configure your log aggregator to parse correlation IDs:

```bash
# View logs
journalctl -u tako-vm -f

# Filter by correlation ID
journalctl -u tako-vm | grep "abc-123-def"
```

## Scaling

### Horizontal Scaling

Run multiple Tako VM instances behind a load balancer:

```
                    ┌─────────────┐
                    │   Nginx     │
                    │   (LB)      │
                    └──────┬──────┘
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
    ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
    │  Tako VM 1  │ │  Tako VM 2  │ │  Tako VM 3  │
    └─────────────┘ └─────────────┘ └─────────────┘
           │               │               │
           └───────────────┼───────────────┘
                           ▼
                    ┌─────────────┐
                    │   Docker    │
                    │   Host      │
                    └─────────────┘
```

### Vertical Scaling

Increase workers per instance:

```yaml
max_workers: 16  # More concurrent executions
```

## Backup & Recovery

### Data to Backup

| Path | Contents | Frequency |
|------|----------|-----------|
| PostgreSQL database | Execution records + DLQ | Daily |
| `/etc/tako-vm/config.yaml` | Configuration | On change |

### Backup Script

```bash
#!/bin/bash
# /opt/tako-vm/backup.sh

BACKUP_DIR=/var/backups/tako-vm
DATE=$(date +%Y%m%d)

mkdir -p $BACKUP_DIR

# Backup database (includes execution records and DLQ)
pg_dump "$TAKO_VM_DATABASE_URL" > "$BACKUP_DIR/executions-$DATE.sql"

# Retain 30 days
find $BACKUP_DIR -mtime +30 -delete
```

### DLQ Maintenance

Periodically review and clean up the dead letter queue:

```bash
# Review failed jobs
curl http://localhost:8000/dlq | jq '.[] | {id, error_type, created_at}'

# Remove old entries (implement in your maintenance script)
curl -X DELETE http://localhost:8000/dlq/1
```

## Checklist

Before going to production:

- [ ] Enable `production_mode: true`
- [ ] Pre-build all required images
- [ ] Configure TLS/HTTPS
- [ ] Set up monitoring for:
  - [ ] Health endpoint
  - [ ] Circuit breaker state
  - [ ] DLQ size
  - [ ] Queue depth
- [ ] Configure log aggregation with correlation ID parsing
- [ ] Set up automated backups
- [ ] Test failure scenarios:
  - [ ] Docker daemon restart
  - [ ] High load (queue full)
  - [ ] OOM kills
- [ ] Document runbooks for:
  - [ ] Circuit breaker open
  - [ ] High DLQ count
  - [ ] Queue backup
