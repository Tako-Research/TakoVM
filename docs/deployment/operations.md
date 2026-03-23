# Operations Runbook

Procedures for diagnosing and recovering from common production issues.

## Diagnostic Endpoints

```bash
# Server health
curl http://localhost:8000/health

# Worker pool status (queue depth, active workers)
curl http://localhost:8000/pool/stats

# Dead letter queue stats
curl http://localhost:8000/dlq/stats
```

## Common Scenarios

### High Queue Depth

**Symptom:** `/pool/stats` shows `queue_depth` growing, jobs take longer to start.

**Diagnosis:**
```bash
# Check current stats
curl -s http://localhost:8000/pool/stats | python3 -m json.tool

# Check if workers are stuck
docker ps --filter "name=tako-" --format "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}"
```

**Recovery:**
1. If workers are idle but queue is full → restart the server
2. If all workers are busy → increase `max_workers` in config and restart
3. If containers are stuck → kill stale containers:
   ```bash
   # Find containers running longer than expected
   docker ps --filter "name=tako-" --format "{{.ID}} {{.RunningFor}}" | grep -E "hours|days"
   ```

### Circuit Breaker Open

**Symptom:** Jobs return 503 immediately. Health endpoint shows `"docker_available": false`.

**Diagnosis:**
```bash
# Check Docker daemon
docker info > /dev/null 2>&1 && echo "Docker OK" || echo "Docker DOWN"

# Check Docker socket permissions
ls -la /var/run/docker.sock
```

**Recovery:**
1. Restart Docker daemon: `sudo systemctl restart docker`
2. Wait for Tako VM health check to detect Docker is back (automatic)
3. If persistent, restart Tako VM server

### Dead Letter Queue Growing

**Symptom:** `/dlq/stats` shows increasing count.

**Diagnosis:**
```bash
# Check DLQ contents
curl -s http://localhost:8000/dlq/stats | python3 -m json.tool
```

**Recovery:**
1. Review DLQ entries for common failure patterns
2. Fix the root cause (usually Docker issues or resource exhaustion)
3. DLQ entries are informational — they indicate jobs that failed due to infrastructure errors (not user code errors)

### Database Connection Failures

**Symptom:** Server returns 500 errors. Logs show PostgreSQL connection errors.

**Diagnosis:**
```bash
# Check if auto-managed PostgreSQL is running
docker ps --filter "name=tako-postgres"

# Test connectivity
pg_isready -h localhost -p 55432
```

**Recovery:**
1. If using auto-managed PostgreSQL: restart Tako VM server (it will recreate the container)
2. If using external PostgreSQL: check connection string in `database_url` config
3. Verify the database is reachable from the Tako VM host

### Disk Full

**Symptom:** Jobs fail with write errors. Docker containers fail to start.

**Diagnosis:**
```bash
# Check disk usage
df -h

# Check Docker disk usage
docker system df
```

**Recovery:**
```bash
# Clean up stopped containers and unused images
docker system prune -f

# Remove old Tako VM artifacts if stored locally
# (location depends on your TAKO_VM_DATA_DIR config)
```

### Out of Memory

**Symptom:** Jobs return `oom` status. Host becomes slow.

**Diagnosis:**
```bash
# Check host memory
free -h

# Check container memory limits
docker stats --no-stream --filter "name=tako-"
```

**Recovery:**
1. Reduce `max_workers` to lower concurrent memory usage
2. Lower per-container memory limits in config (`container_memory_mb`)
3. Add more RAM to the host

## Key Metrics to Monitor

| Metric | Source | Warning Threshold |
|--------|--------|-------------------|
| Queue depth | `/pool/stats` → `queue_depth` | > `max_workers` × 2 |
| DLQ count | `/dlq/stats` → `count` | > 0 (investigate) |
| Docker health | `/health` → `docker_available` | `false` |
| Active workers | `/pool/stats` → `active_workers` | = `max_workers` sustained |
| Disk usage | `df -h` | > 80% |
| Memory usage | `free -h` | > 85% |

## Log Locations

| Deployment | Location |
|------------|----------|
| Direct (systemd) | `journalctl -u tako-vm` |
| Docker Compose | `docker-compose logs -f tako-vm` |
| Kubernetes | `kubectl logs -l app=tako-vm` |
