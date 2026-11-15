#!/usr/bin/env python3

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

BASE_URL = "http://localhost:8000"


@dataclass
class StressTestResult:
    test_name: str
    total_requests: int
    successful: int
    failed: int
    duration_sec: float
    latencies_ms: List[float] = field(default_factory=list)
    errors: Dict[str, int] = field(default_factory=dict)
    backpressure_count: int = 0
    jobs_accepted: int = 0
    memory_samples_mb: List[float] = field(default_factory=list)
    throughput_samples: List[float] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return (self.successful / self.total_requests * 100) if self.total_requests > 0 else 0

    @property
    def acceptance_rate(self) -> float:
        total_submissions = self.jobs_accepted + self.backpressure_count
        return (self.jobs_accepted / total_submissions * 100) if total_submissions > 0 else 100

    @property
    def throughput(self) -> float:
        return self.total_requests / self.duration_sec if self.duration_sec > 0 else 0

    @property
    def avg_latency_ms(self) -> float:
        return statistics.mean(self.latencies_ms) if self.latencies_ms else 0

    @property
    def p50_latency_ms(self) -> float:
        return statistics.median(self.latencies_ms) if self.latencies_ms else 0

    @property
    def p95_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0
        sorted_latencies = sorted(self.latencies_ms)
        idx = int(len(sorted_latencies) * 0.95)
        return sorted_latencies[idx]

    @property
    def p99_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0
        sorted_latencies = sorted(self.latencies_ms)
        idx = int(len(sorted_latencies) * 0.99)
        return sorted_latencies[min(idx, len(sorted_latencies) - 1)]

    @property
    def memory_growth_pct(self) -> float:
        if len(self.memory_samples_mb) < 2:
            return 0
        start = self.memory_samples_mb[0]
        end = self.memory_samples_mb[-1]
        return ((end - start) / start * 100) if start > 0 else 0

    @property
    def throughput_stability(self) -> float:
        if len(self.throughput_samples) < 2:
            return 0
        mean = statistics.mean(self.throughput_samples)
        if mean == 0:
            return 0
        stdev = statistics.stdev(self.throughput_samples)
        return (stdev / mean) * 100

    def print_summary(self):
        print(f"\n{'=' * 60}")
        print(f"Test: {self.test_name}")
        print(f"{'=' * 60}")
        print(f"Total Requests:    {self.total_requests}")
        print(f"Successful:        {self.successful}")
        print(f"Failed:            {self.failed}")
        print(f"Success Rate:      {self.success_rate:.1f}%")
        print(f"Duration:          {self.duration_sec:.2f}s")
        print(f"Throughput:        {self.throughput:.1f} req/s")
        print(f"Avg Latency:       {self.avg_latency_ms:.1f}ms")
        print(f"P50 Latency:       {self.p50_latency_ms:.1f}ms")
        print(f"P95 Latency:       {self.p95_latency_ms:.1f}ms")
        print(f"P99 Latency:       {self.p99_latency_ms:.1f}ms")

        if self.backpressure_count > 0 or self.jobs_accepted > 0:
            print("\n--- Capacity Metrics ---")
            print(f"Jobs Accepted:     {self.jobs_accepted}")
            print(f"Backpressure(503): {self.backpressure_count}")
            print(f"Acceptance Rate:   {self.acceptance_rate:.1f}%")

        if self.memory_samples_mb:
            print("\n--- Memory Metrics ---")
            print(f"Memory Start:      {self.memory_samples_mb[0]:.1f} MB")
            print(f"Memory End:        {self.memory_samples_mb[-1]:.1f} MB")
            print(f"Memory Growth:     {self.memory_growth_pct:.1f}%")

        if self.throughput_samples:
            print("\n--- Stability Metrics ---")
            print(f"Throughput Min:    {min(self.throughput_samples):.1f} req/s")
            print(f"Throughput Max:    {max(self.throughput_samples):.1f} req/s")
            print(f"Throughput CV:     {self.throughput_stability:.1f}%")

        if self.errors:
            print("\n--- Errors ---")
            for error, count in sorted(self.errors.items(), key=lambda x: -x[1]):
                print(f"  {error}: {count}")


async def make_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    json_data: Optional[Dict[str, Any]] = None,
) -> tuple[bool, float, str]:
    start = time.perf_counter()
    try:
        if method == "GET":
            resp = await client.get(url)
        else:
            resp = await client.post(url, json=json_data)

        latency_ms = (time.perf_counter() - start) * 1000

        if resp.status_code < 400:
            return True, latency_ms, ""
        else:
            error = f"HTTP {resp.status_code}"
            return False, latency_ms, error
    except httpx.TimeoutException:
        latency_ms = (time.perf_counter() - start) * 1000
        return False, latency_ms, "timeout"
    except httpx.HTTPError as e:
        latency_ms = (time.perf_counter() - start) * 1000
        return False, latency_ms, str(type(e).__name__)
    except Exception as e:
        latency_ms = (time.perf_counter() - start) * 1000
        return False, latency_ms, str(e)[:50]


async def stress_test_health(
    num_requests: int = 1000,
    concurrency: int = 50,
) -> StressTestResult:
    result = StressTestResult(
        test_name=f"Health Check ({num_requests} requests, {concurrency} concurrent)",
        total_requests=num_requests,
        successful=0,
        failed=0,
        duration_sec=0,
    )

    semaphore = asyncio.Semaphore(concurrency)

    async def bounded_request(client):
        async with semaphore:
            return await make_request(client, "GET", f"{BASE_URL}/health")

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(30.0)

    start = time.perf_counter()
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        tasks = [bounded_request(client) for _ in range(num_requests)]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    result.duration_sec = time.perf_counter() - start

    for resp in responses:
        if isinstance(resp, BaseException):
            result.failed += 1
            error = str(type(resp).__name__)
            result.errors[error] = result.errors.get(error, 0) + 1
        else:
            success, latency_ms, error = resp
            result.latencies_ms.append(latency_ms)
            if success:
                result.successful += 1
            else:
                result.failed += 1
                result.errors[error] = result.errors.get(error, 0) + 1

    return result


async def stress_test_sync_execution(
    num_requests: int = 50,
    concurrency: int = 10,
) -> StressTestResult:
    result = StressTestResult(
        test_name=f"Sync Execution ({num_requests} requests, {concurrency} concurrent)",
        total_requests=num_requests,
        successful=0,
        failed=0,
        duration_sec=0,
    )

    semaphore = asyncio.Semaphore(concurrency)

    async def bounded_request(client, i):
        async with semaphore:
            return await make_request(
                client, "POST", f"{BASE_URL}/execute", json_data={"code": f"print({i} * 2)"}
            )

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(120.0)

    start = time.perf_counter()
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        tasks = [bounded_request(client, i) for i in range(num_requests)]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    result.duration_sec = time.perf_counter() - start

    for resp in responses:
        if isinstance(resp, BaseException):
            result.failed += 1
            error = str(type(resp).__name__)
            result.errors[error] = result.errors.get(error, 0) + 1
        else:
            success, latency_ms, error = resp
            result.latencies_ms.append(latency_ms)
            if success:
                result.successful += 1
            else:
                result.failed += 1
                result.errors[error] = result.errors.get(error, 0) + 1

    return result


async def stress_test_async_execution(
    num_requests: int = 100,
    concurrency: int = 20,
) -> StressTestResult:
    result = StressTestResult(
        test_name=f"Async Submission ({num_requests} requests, {concurrency} concurrent)",
        total_requests=num_requests,
        successful=0,
        failed=0,
        duration_sec=0,
    )

    semaphore = asyncio.Semaphore(concurrency)
    job_ids = []

    async def bounded_request(client, i):
        async with semaphore:
            start = time.perf_counter()
            try:
                resp = await client.post(
                    f"{BASE_URL}/execute/async",
                    json={"code": f"import time; time.sleep(0.1); print({i})"},
                )
                latency_ms = (time.perf_counter() - start) * 1000
                if resp.status_code < 400:
                    data = resp.json()
                    job_ids.append(data.get("job_id"))
                    return True, latency_ms, ""
                else:
                    return False, latency_ms, f"HTTP {resp.status_code}"
            except Exception as e:
                latency_ms = (time.perf_counter() - start) * 1000
                return False, latency_ms, str(e)[:50]

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(30.0)

    start = time.perf_counter()
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        tasks = [bounded_request(client, i) for i in range(num_requests)]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    result.duration_sec = time.perf_counter() - start

    for resp in responses:
        if isinstance(resp, BaseException):
            result.failed += 1
            error = str(type(resp).__name__)
            result.errors[error] = result.errors.get(error, 0) + 1
        else:
            success, latency_ms, error = resp
            result.latencies_ms.append(latency_ms)
            if success:
                result.successful += 1
            else:
                result.failed += 1
                result.errors[error] = result.errors.get(error, 0) + 1

    print(f"  Submitted {len(job_ids)} jobs to queue")
    return result


async def stress_test_queue_flood(
    num_requests: int = 200,
    concurrency: int = 50,
) -> StressTestResult:
    result = StressTestResult(
        test_name=f"Queue Flood ({num_requests} requests, {concurrency} concurrent)",
        total_requests=num_requests,
        successful=0,
        failed=0,
        duration_sec=0,
    )

    semaphore = asyncio.Semaphore(concurrency)
    accepted = 0
    rejected = 0

    async def bounded_request(client, i):
        nonlocal accepted, rejected
        async with semaphore:
            start = time.perf_counter()
            try:
                resp = await client.post(
                    f"{BASE_URL}/execute/async",
                    json={"code": f"import time; time.sleep(2); print({i})"},
                )
                latency_ms = (time.perf_counter() - start) * 1000
                if resp.status_code == 200:
                    accepted += 1
                    return True, latency_ms, ""
                elif resp.status_code == 503:
                    rejected += 1
                    return False, latency_ms, "queue_full"
                else:
                    return False, latency_ms, f"HTTP {resp.status_code}"
            except Exception as e:
                latency_ms = (time.perf_counter() - start) * 1000
                return False, latency_ms, str(e)[:50]

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(30.0)

    start = time.perf_counter()
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        tasks = [bounded_request(client, i) for i in range(num_requests)]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    result.duration_sec = time.perf_counter() - start

    for resp in responses:
        if isinstance(resp, BaseException):
            result.failed += 1
            error = str(type(resp).__name__)
            result.errors[error] = result.errors.get(error, 0) + 1
        else:
            success, latency_ms, error = resp
            result.latencies_ms.append(latency_ms)
            if success:
                result.successful += 1
            else:
                result.failed += 1
                result.errors[error] = result.errors.get(error, 0) + 1

    print(f"  Accepted: {accepted}, Rejected (503): {rejected}")
    return result


async def stress_test_idempotency(
    num_duplicate_requests: int = 50,
    concurrency: int = 10,
) -> StressTestResult:
    result = StressTestResult(
        test_name=f"Idempotency ({num_duplicate_requests} duplicate requests, {concurrency} concurrent)",
        total_requests=num_duplicate_requests,
        successful=0,
        failed=0,
        duration_sec=0,
    )

    idempotency_key = f"stress-test-{int(time.time())}"
    job_ids = set()
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded_request(client, i):
        async with semaphore:
            start = time.perf_counter()
            try:
                resp = await client.post(
                    f"{BASE_URL}/execute/async",
                    json={"code": "print('idempotent')", "idempotency_key": idempotency_key},
                )
                latency_ms = (time.perf_counter() - start) * 1000
                if resp.status_code < 400:
                    data = resp.json()
                    job_ids.add(data.get("job_id"))
                    return True, latency_ms, ""
                else:
                    return False, latency_ms, f"HTTP {resp.status_code}"
            except Exception as e:
                latency_ms = (time.perf_counter() - start) * 1000
                return False, latency_ms, str(e)[:50]

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(30.0)

    start = time.perf_counter()
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        tasks = [bounded_request(client, i) for i in range(num_duplicate_requests)]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    result.duration_sec = time.perf_counter() - start

    for resp in responses:
        if isinstance(resp, BaseException):
            result.failed += 1
            error = str(type(resp).__name__)
            result.errors[error] = result.errors.get(error, 0) + 1
        else:
            success, latency_ms, error = resp
            result.latencies_ms.append(latency_ms)
            if success:
                result.successful += 1
            else:
                result.failed += 1
                result.errors[error] = result.errors.get(error, 0) + 1

    print(f"  Unique job IDs returned: {len(job_ids)} (expected: 1)")
    if len(job_ids) == 1:
        print("  Idempotency working correctly!")
    else:
        print(f"  WARNING: Idempotency violation - got {len(job_ids)} different job IDs")
        result.errors["idempotency_violation"] = len(job_ids) - 1

    return result


async def stress_test_mixed_workload(
    duration_sec: int = 30,
    concurrency: int = 20,
) -> StressTestResult:
    result = StressTestResult(
        test_name=f"Mixed Workload ({duration_sec}s, {concurrency} concurrent)",
        total_requests=0,
        successful=0,
        failed=0,
        duration_sec=duration_sec,
    )

    semaphore = asyncio.Semaphore(concurrency)
    stop_event = asyncio.Event()
    job_ids = []
    lock = asyncio.Lock()

    async def health_worker(client):
        while not stop_event.is_set():
            async with semaphore:
                success, latency_ms, error = await make_request(client, "GET", f"{BASE_URL}/health")
                async with lock:
                    result.total_requests += 1
                    result.latencies_ms.append(latency_ms)
                    if success:
                        result.successful += 1
                    else:
                        result.failed += 1
                        result.errors[error] = result.errors.get(error, 0) + 1
            await asyncio.sleep(0.01)

    async def submit_worker(client):
        i = 0
        while not stop_event.is_set():
            async with semaphore:
                start = time.perf_counter()
                try:
                    resp = await client.post(
                        f"{BASE_URL}/execute/async", json={"code": f"print({i})"}
                    )
                    latency_ms = (time.perf_counter() - start) * 1000
                    async with lock:
                        result.total_requests += 1
                        result.latencies_ms.append(latency_ms)
                        if resp.status_code < 400:
                            result.successful += 1
                            result.jobs_accepted += 1
                            data = resp.json()
                            job_ids.append(data.get("job_id"))
                        elif resp.status_code == 503:
                            result.successful += 1
                            result.backpressure_count += 1
                        else:
                            result.failed += 1
                            result.errors[f"HTTP {resp.status_code}"] = (
                                result.errors.get(f"HTTP {resp.status_code}", 0) + 1
                            )
                except Exception as e:
                    async with lock:
                        result.failed += 1
                        result.total_requests += 1
                        error = str(type(e).__name__)
                        result.errors[error] = result.errors.get(error, 0) + 1
            i += 1
            await asyncio.sleep(0.1)

    async def poll_worker(client):
        while not stop_event.is_set():
            if job_ids:
                job_id = job_ids[-1]
                async with semaphore:
                    success, latency_ms, error = await make_request(
                        client, "GET", f"{BASE_URL}/jobs/{job_id}"
                    )
                    async with lock:
                        result.total_requests += 1
                        result.latencies_ms.append(latency_ms)
                        if success:
                            result.successful += 1
                        else:
                            result.failed += 1
                            result.errors[error] = result.errors.get(error, 0) + 1
            await asyncio.sleep(0.05)

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(30.0)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        workers = [
            asyncio.create_task(health_worker(client)),
            asyncio.create_task(health_worker(client)),
            asyncio.create_task(submit_worker(client)),
            asyncio.create_task(submit_worker(client)),
            asyncio.create_task(poll_worker(client)),
            asyncio.create_task(poll_worker(client)),
        ]

        await asyncio.sleep(duration_sec)
        stop_event.set()

        await asyncio.gather(*workers, return_exceptions=True)

    return result


async def stress_test_baseline(
    num_requests: int = 100,
) -> StressTestResult:
    result = StressTestResult(
        test_name=f"Baseline (Sequential, {num_requests} requests)",
        total_requests=num_requests,
        successful=0,
        failed=0,
        duration_sec=0,
    )

    limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
    timeout = httpx.Timeout(30.0)

    start = time.perf_counter()
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        for i in range(num_requests):
            req_start = time.perf_counter()
            try:
                resp = await client.post(f"{BASE_URL}/execute/async", json={"code": f"print({i})"})
                latency_ms = (time.perf_counter() - req_start) * 1000
                result.latencies_ms.append(latency_ms)
                if resp.status_code < 400:
                    result.successful += 1
                    result.jobs_accepted += 1
                elif resp.status_code == 503:
                    result.successful += 1
                    result.backpressure_count += 1
                else:
                    result.failed += 1
                    result.errors[f"HTTP {resp.status_code}"] = (
                        result.errors.get(f"HTTP {resp.status_code}", 0) + 1
                    )
            except Exception as e:
                latency_ms = (time.perf_counter() - req_start) * 1000
                result.latencies_ms.append(latency_ms)
                result.failed += 1
                error = str(type(e).__name__)
                result.errors[error] = result.errors.get(error, 0) + 1

    result.duration_sec = time.perf_counter() - start
    return result


async def get_server_memory_mb() -> Optional[float]:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BASE_URL}/health", timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                if "memory_mb" in data:
                    return data["memory_mb"]
    except Exception:
        pass
    return None


async def stress_test_soak(
    duration_sec: int = 300,
    concurrency: int = 10,
    sample_interval_sec: int = 10,
) -> StressTestResult:
    result = StressTestResult(
        test_name=f"Soak Test ({duration_sec}s, {concurrency} concurrent)",
        total_requests=0,
        successful=0,
        failed=0,
        duration_sec=duration_sec,
    )

    semaphore = asyncio.Semaphore(concurrency)
    stop_event = asyncio.Event()
    lock = asyncio.Lock()

    interval_requests = 0
    interval_start = time.perf_counter()

    async def submit_worker(client):
        nonlocal interval_requests
        i = 0
        while not stop_event.is_set():
            async with semaphore:
                start = time.perf_counter()
                try:
                    resp = await client.post(
                        f"{BASE_URL}/execute/async", json={"code": f"print({i})"}
                    )
                    latency_ms = (time.perf_counter() - start) * 1000
                    async with lock:
                        result.total_requests += 1
                        interval_requests += 1
                        result.latencies_ms.append(latency_ms)
                        if resp.status_code < 400:
                            result.successful += 1
                            result.jobs_accepted += 1
                        elif resp.status_code == 503:
                            result.successful += 1
                            result.backpressure_count += 1
                        else:
                            result.failed += 1
                            result.errors[f"HTTP {resp.status_code}"] = (
                                result.errors.get(f"HTTP {resp.status_code}", 0) + 1
                            )
                except Exception as e:
                    async with lock:
                        result.failed += 1
                        result.total_requests += 1
                        error = str(type(e).__name__)
                        result.errors[error] = result.errors.get(error, 0) + 1
            i += 1
            await asyncio.sleep(0.05)

    async def sampler():
        nonlocal interval_requests, interval_start
        while not stop_event.is_set():
            await asyncio.sleep(sample_interval_sec)
            if stop_event.is_set():
                break

            async with lock:
                elapsed = time.perf_counter() - interval_start
                if elapsed > 0:
                    throughput = interval_requests / elapsed
                    result.throughput_samples.append(throughput)

                interval_requests = 0
                interval_start = time.perf_counter()

            mem = await get_server_memory_mb()
            if mem is not None:
                result.memory_samples_mb.append(mem)

            elapsed_total = len(result.throughput_samples) * sample_interval_sec
            print(
                f"  [{elapsed_total}s] Throughput: {result.throughput_samples[-1]:.1f} req/s, "
                f"Total: {result.total_requests}, Backpressure: {result.backpressure_count}"
            )

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(30.0)

    mem = await get_server_memory_mb()
    if mem is not None:
        result.memory_samples_mb.append(mem)

    print(f"  Starting soak test ({duration_sec}s)...")

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        workers = [asyncio.create_task(submit_worker(client)) for _ in range(concurrency // 2 + 1)]
        workers.append(asyncio.create_task(sampler()))

        await asyncio.sleep(duration_sec)
        stop_event.set()

        await asyncio.gather(*workers, return_exceptions=True)

    return result


async def stress_test_capacity(
    target_rate: int = 50,
    duration_sec: int = 60,
) -> StressTestResult:
    result = StressTestResult(
        test_name=f"Capacity Test ({target_rate} req/s target, {duration_sec}s)",
        total_requests=0,
        successful=0,
        failed=0,
        duration_sec=duration_sec,
    )

    stop_event = asyncio.Event()
    lock = asyncio.Lock()
    delay = 1.0 / target_rate

    async def rate_limited_submit(client):
        i = 0
        while not stop_event.is_set():
            start = time.perf_counter()
            try:
                resp = await client.post(f"{BASE_URL}/execute/async", json={"code": f"print({i})"})
                latency_ms = (time.perf_counter() - start) * 1000
                async with lock:
                    result.total_requests += 1
                    result.latencies_ms.append(latency_ms)
                    if resp.status_code < 400:
                        result.successful += 1
                        result.jobs_accepted += 1
                    elif resp.status_code == 503:
                        result.successful += 1
                        result.backpressure_count += 1
                    else:
                        result.failed += 1
                        result.errors[f"HTTP {resp.status_code}"] = (
                            result.errors.get(f"HTTP {resp.status_code}", 0) + 1
                        )
            except Exception as e:
                async with lock:
                    result.failed += 1
                    result.total_requests += 1
                    error = str(type(e).__name__)
                    result.errors[error] = result.errors.get(error, 0) + 1

            i += 1
            elapsed = time.perf_counter() - start
            sleep_time = max(0, delay - elapsed)
            await asyncio.sleep(sleep_time)

    limits = httpx.Limits(max_connections=100, max_keepalive_connections=50)
    timeout = httpx.Timeout(30.0)

    print(f"  Targeting {target_rate} req/s for {duration_sec}s...")

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        workers = [
            asyncio.create_task(rate_limited_submit(client))
            for _ in range(min(target_rate // 10 + 1, 10))
        ]

        await asyncio.sleep(duration_sec)
        stop_event.set()
        await asyncio.gather(*workers, return_exceptions=True)

    return result


async def check_server_health():
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BASE_URL}/health", timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                print(f"Server is healthy: {data.get('status')}")
                print(f"Docker available: {data.get('docker_available')}")
                print(f"Queue stats: {data.get('queue_stats')}")
                return True
    except Exception as e:
        print(f"Server not reachable: {e}")
    return False


async def main():
    parser = argparse.ArgumentParser(description="Tako VM Stress Test")
    parser.add_argument("--quick", action="store_true", help="Run quick test suite")
    parser.add_argument("--full", action="store_true", help="Run full test suite")
    parser.add_argument(
        "--enterprise", action="store_true", help="Run enterprise test suite (soak, capacity)"
    )
    parser.add_argument(
        "--soak-duration",
        type=int,
        default=300,
        help="Soak test duration in seconds (default: 300)",
    )
    parser.add_argument(
        "--test",
        type=str,
        help="Run specific test: health, sync, async, flood, idempotency, mixed, baseline, soak, capacity",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Tako VM Stress Test")
    print("=" * 60)

    if not await check_server_health():
        print("\nERROR: Server not running. Start with: docker-compose up -d")
        return

    print()
    results = []

    if args.test:
        test_map = {
            "health": lambda: stress_test_health(1000, 50),
            "sync": lambda: stress_test_sync_execution(20, 5),
            "async": lambda: stress_test_async_execution(100, 20),
            "flood": lambda: stress_test_queue_flood(200, 50),
            "idempotency": lambda: stress_test_idempotency(50, 20),
            "mixed": lambda: stress_test_mixed_workload(30, 20),
            "baseline": lambda: stress_test_baseline(100),
            "soak": lambda: stress_test_soak(args.soak_duration, 10),
            "capacity": lambda: stress_test_capacity(50, 60),
        }
        if args.test in test_map:
            print(f"Running {args.test} test...")
            result = await test_map[args.test]()
            results.append(result)
        else:
            print(f"Unknown test: {args.test}")
            print(f"Available: {', '.join(test_map.keys())}")
            return
    elif args.quick:
        print("Running quick test suite...")
        results.append(await stress_test_health(500, 25))
        results.append(await stress_test_async_execution(50, 10))
        results.append(await stress_test_idempotency(20, 10))
    elif args.full:
        print("Running full test suite...")
        results.append(await stress_test_health(2000, 100))
        results.append(await stress_test_sync_execution(50, 10))
        results.append(await stress_test_async_execution(200, 30))
        results.append(await stress_test_queue_flood(300, 50))
        results.append(await stress_test_idempotency(100, 30))
        results.append(await stress_test_mixed_workload(60, 30))
    elif args.enterprise:
        print("Running enterprise test suite...")
        print("\n[1/4] Baseline throughput (single connection)...")
        results.append(await stress_test_baseline(100))
        print("\n[2/4] Mixed workload with backpressure tracking...")
        results.append(await stress_test_mixed_workload(60, 20))
        print("\n[3/4] Capacity test (sustained rate)...")
        results.append(await stress_test_capacity(30, 60))
        print(f"\n[4/4] Soak test ({args.soak_duration}s)...")
        results.append(await stress_test_soak(args.soak_duration, 10))
    else:
        print("Running default test suite...")
        results.append(await stress_test_health(1000, 50))
        results.append(await stress_test_async_execution(100, 20))
        results.append(await stress_test_idempotency(50, 20))
        results.append(await stress_test_mixed_workload(30, 20))

    for result in results:
        result.print_summary()

    print("\n" + "=" * 60)
    print("OVERALL SUMMARY")
    print("=" * 60)
    total_requests = sum(r.total_requests for r in results)
    total_successful = sum(r.successful for r in results)
    total_failed = sum(r.failed for r in results)
    all_latencies = [lat for r in results for lat in r.latencies_ms]

    print(f"Total Requests:    {total_requests}")
    print(f"Successful:        {total_successful}")
    print(f"Failed:            {total_failed}")
    print(
        f"Overall Success:   {total_successful / total_requests * 100:.1f}%"
        if total_requests
        else "N/A"
    )

    if all_latencies:
        print(f"Overall Avg Lat:   {statistics.mean(all_latencies):.1f}ms")
        sorted_all = sorted(all_latencies)
        print(f"Overall P95 Lat:   {sorted_all[int(len(sorted_all) * 0.95)]:.1f}ms")

    critical_issues = []
    warnings = []
    for r in results:
        if r.success_rate < 95:
            critical_issues.append(f"{r.test_name}: Success rate below 95% ({r.success_rate:.1f}%)")
        if "idempotency_violation" in r.errors:
            critical_issues.append(f"{r.test_name}: Idempotency violations detected")

        if r.acceptance_rate < 95 and r.backpressure_count > 0:
            warnings.append(
                f"{r.test_name}: High backpressure - only {r.acceptance_rate:.1f}% jobs accepted"
            )
        if r.memory_growth_pct > 10:
            warnings.append(
                f"{r.test_name}: Memory grew {r.memory_growth_pct:.1f}% (potential leak)"
            )
        if r.throughput_stability > 30:
            warnings.append(
                f"{r.test_name}: Unstable throughput (CV={r.throughput_stability:.1f}%)"
            )
        if r.p99_latency_ms > 500:
            warnings.append(f"{r.test_name}: P99 latency exceeds 500ms ({r.p99_latency_ms:.1f}ms)")

    if critical_issues:
        print("\nCRITICAL ISSUES:")
        for issue in critical_issues:
            print(f"  - {issue}")

    if warnings:
        print("\nWARNINGS:")
        for warning in warnings:
            print(f"  - {warning}")

    if not critical_issues and not warnings:
        print("\nAll tests passed within acceptable parameters.")


if __name__ == "__main__":
    asyncio.run(main())
