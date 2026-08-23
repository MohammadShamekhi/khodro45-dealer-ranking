#!/usr/bin/env python3
"""Async load test for GET /api/v1/auctions/?ordering=suggested.

Load pattern (fixed concurrency, closed workload):
  - Schedule all N requests immediately at t=0.
  - At most `--concurrency` requests are in-flight at once (asyncio semaphore).
  - When one finishes, the next queued request starts — no ramp-up, no fixed RPS cap.
  - This is an open-loop worker pool until all N requests complete.

Example:
  export KHODRO45_AUTHORIZATION='Token 1dbc0ff4...'
  export LOAD_TEST_DEALER_ID=451301
  python scripts/load_test_auctions.py --target khodro45 --requests 200 --concurrency 20
  python scripts/load_test_auctions.py --target gateway --requests 200 --concurrency 20
  python scripts/load_test_auctions.py --target bmw --requests 200 --concurrency 20

"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

DEFAULT_RESULTS_DIR = Path("load_test_results")

# Use ordering=suggested on every target that supports it.
TARGET_URLS: dict[str, str] = {
    "khodro45": "https://khodro45.com/api/v1/auctions/?ordering=suggested",
    "gateway": "https://dealerintelligence.darkube.ir/api/v1/suggested_full/",
    "gateway-ids": "https://dealerintelligence.darkube.ir/api/v1/suggested_ids/",
    "gateway-live": "https://dealerintelligence.darkube.ir/api/v1/live/",
    "bmw": "https://bmw.technical45.com/api/v1/auctions/?ordering=suggested",
}
DEFAULT_URL = TARGET_URLS["bmw"]


def _strip_env_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1].strip()
    return v


def _token_authorization(raw: str) -> str:
    """Khodro45/BMW expect ``Authorization: Token <hex>``."""
    v = _strip_env_quotes(raw)
    if not v:
        return ""
    if v.lower().startswith("token "):
        return v
    return f"Token {v}"


def _env(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped if stripped else None


def endpoint_file_slug(url: str) -> str:
    """Derive a stable filename stem from the URL path (and ordering query if present)."""
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    slug = parts[-1] if parts else "root"
    ordering = parse_qs(parsed.query).get("ordering", [None])[0]
    if ordering:
        slug = f"{slug}_{ordering}"
    slug = re.sub(r"[^\w-]", "_", slug).strip("_")
    return slug or "load_test"


def resolve_authorization(target: str, explicit: str) -> str:
    if explicit.strip():
        return _token_authorization(explicit)
    if target == "khodro45":
        for env_name in ("KHODRO45_AUTHORIZATION", "LOAD_TEST_AUTHORIZATION"):
            raw = _env(env_name)
            if raw:
                return _token_authorization(raw)
    elif target == "bmw":
        raw = _env("LOAD_TEST_AUTHORIZATION")
        if raw:
            return _token_authorization(raw)
    return ""

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:151.0) Gecko/20100101 Firefox/151.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Referer": "https://dealer.bmw.technical45.com/",
    "Origin": "https://dealer.bmw.technical45.com",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "Connection": "keep-alive",
    "TE": "trailers",
}


@dataclass
class RequestResult:
    ok: bool
    status_code: int
    latency_ms: float
    started_offset_s: float = 0.0
    completed_offset_s: float = 0.0
    error: str = ""


@dataclass
class LoadTestReport:
    total: int = 0
    success: int = 0
    failed: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    timeline: list[RequestResult] = field(default_factory=list)
    status_counts: dict[int, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    duration_s: float = 0.0

    def add(self, result: RequestResult) -> None:
        self.total += 1
        self.latencies_ms.append(result.latency_ms)
        self.timeline.append(result)
        self.status_counts[result.status_code] = self.status_counts.get(result.status_code, 0) + 1
        if result.ok:
            self.success += 1
        else:
            self.failed += 1
            key = result.error or f"HTTP {result.status_code}"
            self.errors[key] = self.errors.get(key, 0) + 1


async def one_request(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    semaphore: asyncio.Semaphore,
    test_started: float,
) -> RequestResult:
    async with semaphore:
        started = time.perf_counter()
        started_offset_s = started - test_started
        try:
            response = await client.get(url, headers=headers)
            completed = time.perf_counter()
            latency_ms = (completed - started) * 1000.0
            ok = 200 <= response.status_code < 300
            error = ""
            if not ok:
                try:
                    body = response.json()
                    if isinstance(body, dict) and body.get("detail"):
                        error = str(body["detail"])
                except Exception:
                    pass
                if not error:
                    error = response.reason_phrase or "request failed"
            return RequestResult(
                ok=ok,
                status_code=response.status_code,
                latency_ms=latency_ms,
                started_offset_s=started_offset_s,
                completed_offset_s=completed - test_started,
                error=error,
            )
        except httpx.HTTPError as exc:
            completed = time.perf_counter()
            latency_ms = (completed - started) * 1000.0
            return RequestResult(
                ok=False,
                status_code=0,
                latency_ms=latency_ms,
                started_offset_s=started_offset_s,
                completed_offset_s=completed - test_started,
                error=str(exc),
            )


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[idx]


def print_report(report: LoadTestReport) -> None:
    print("\n=== Load test results ===")
    print(f"Duration:     {report.duration_s:.2f}s")
    print(f"Total:        {report.total}")
    print(f"Success:      {report.success}")
    print(f"Failed:       {report.failed}")
    if report.duration_s > 0:
        print(f"Throughput:   {report.total / report.duration_s:.1f} req/s")

    if report.latencies_ms:
        print("\nLatency (ms):")
        print(f"  min:  {min(report.latencies_ms):.0f}")
        print(f"  avg:  {statistics.mean(report.latencies_ms):.0f}")
        print(f"  p50:  {percentile(report.latencies_ms, 50):.0f}")
        print(f"  p90:  {percentile(report.latencies_ms, 90):.0f}")
        print(f"  p95:  {percentile(report.latencies_ms, 95):.0f}")
        print(f"  p99:  {percentile(report.latencies_ms, 99):.0f}")
        print(f"  max:  {max(report.latencies_ms):.0f}")

    if report.status_counts:
        print("\nStatus codes:")
        for code in sorted(report.status_counts):
            print(f"  {code}: {report.status_counts[code]}")

    if report.errors:
        print("\nErrors:")
        for msg, count in sorted(report.errors.items(), key=lambda x: -x[1]):
            print(f"  [{count}x] {msg}")


def print_load_pattern(total_requests: int, concurrency: int) -> None:
    print("\n=== Load pattern ===")
    print("Model:       closed workload, fixed concurrency (worker pool)")
    print(f"Requests:    {total_requests} total, all queued at t=0")
    print(f"Concurrency: max {concurrency} in-flight at any time")
    print("Behavior:    start up to concurrency requests immediately;")
    print("             each completion starts the next queued request")
    print("Not used:    ramp-up, fixed RPS throttle, think time")


def run_stamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")



def chart_title(base: str, url: str) -> str:
    return f"{base}\n{url}"


def save_latency_histogram(
    report: LoadTestReport,
    *,
    output_path: Path,
    url: str,
    concurrency: int,
) -> Path:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise SystemExit("Histogram requires matplotlib: pip install matplotlib") from exc

    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    latencies = report.latencies_ms
    if not latencies:
        raise SystemExit("No latency samples to plot.")

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.hist(latencies, bins="auto", color="#ff9532", edgecolor="#4f5866", alpha=0.9)
    ax.set_xlabel("Latency (ms)")
    ax.set_ylabel("Requests")
    ax.set_title(chart_title("Load test latency histogram", url), fontsize=11)
    ax.grid(axis="y", alpha=0.3)

    p50 = percentile(latencies, 50)
    p95 = percentile(latencies, 95)
    ax.axvline(p50, color="#2563eb", linestyle="--", linewidth=1.5, label=f"p50 = {p50:.0f} ms")
    ax.axvline(p95, color="#dc2626", linestyle="--", linewidth=1.5, label=f"p95 = {p95:.0f} ms")
    ax.legend(loc="upper right")

    throughput = report.total / report.duration_s if report.duration_s > 0 else 0.0
    subtitle = f"n={report.total}, concurrency={concurrency}, throughput={throughput:.1f} req/s"
    fig.text(0.5, 0.02, subtitle, ha="center", va="bottom", fontsize=8)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return output_path


def _in_flight_series(timeline: list[RequestResult]) -> tuple[list[float], list[int]]:
    events: list[tuple[float, int]] = []
    for item in timeline:
        events.append((item.started_offset_s, 1))
        events.append((item.completed_offset_s, -1))
    events.sort(key=lambda x: (x[0], -x[1]))
    times: list[float] = []
    counts: list[int] = []
    in_flight = 0
    for offset_s, delta in events:
        in_flight += delta
        times.append(offset_s)
        counts.append(in_flight)
    return times, counts


def _completed_rps_series(timeline: list[RequestResult], bucket_s: float = 1.0) -> tuple[list[float], list[int]]:
    if not timeline:
        return [], []
    end_s = max(item.completed_offset_s for item in timeline)
    bucket_count = max(1, int(end_s // bucket_s) + 1)
    buckets = [0] * bucket_count
    for item in timeline:
        idx = min(int(item.completed_offset_s // bucket_s), bucket_count - 1)
        buckets[idx] += 1
    centers = [(i + 0.5) * bucket_s for i in range(bucket_count)]
    return centers, buckets


def save_time_series_plot(
    report: LoadTestReport,
    *,
    output_path: Path,
    url: str,
    concurrency: int,
) -> Path:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise SystemExit("Time series plot requires matplotlib: pip install matplotlib") from exc

    timeline = report.timeline
    if not timeline:
        raise SystemExit("No timeline samples to plot.")

    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(3, 1, figsize=(11, 10), sharex=True)
    fig.suptitle(chart_title("Load test time series", url), fontsize=11, y=0.995)

    flight_t, flight_n = _in_flight_series(timeline)
    axes[0].step(flight_t, flight_n, where="post", color="#2563eb", linewidth=1.5)
    axes[0].axhline(concurrency, color="#dc2626", linestyle="--", linewidth=1, label=f"limit = {concurrency}")
    axes[0].set_ylabel("In-flight")
    axes[0].set_title("Load pattern: in-flight requests over time")
    axes[0].grid(alpha=0.3)
    axes[0].legend(loc="upper right")

    ok_x = [item.completed_offset_s for item in timeline if item.ok]
    ok_y = [item.latency_ms for item in timeline if item.ok]
    bad_x = [item.completed_offset_s for item in timeline if not item.ok]
    bad_y = [item.latency_ms for item in timeline if not item.ok]
    axes[1].scatter(ok_x, ok_y, s=12, alpha=0.7, color="#ff9532", label="success")
    if bad_x:
        axes[1].scatter(bad_x, bad_y, s=18, alpha=0.9, color="#dc2626", label="failed")
    axes[1].set_ylabel("Latency (ms)")
    axes[1].set_title("Per-request latency at completion time")
    axes[1].grid(alpha=0.3)
    axes[1].legend(loc="upper right")

    rps_x, rps_y = _completed_rps_series(timeline, bucket_s=1.0)
    axes[2].bar(rps_x, rps_y, width=0.9, color="#4f5866", alpha=0.85)
    axes[2].set_xlabel("Elapsed time (s)")
    axes[2].set_ylabel("Completed / s")
    axes[2].set_title("Completion rate (1 s buckets)")
    axes[2].grid(axis="y", alpha=0.3)

    throughput = report.total / report.duration_s if report.duration_s > 0 else 0.0
    subtitle = (
        f"closed workload · max concurrency={concurrency} · n={report.total} · "
        f"throughput={throughput:.1f} req/s"
    )
    fig.text(0.5, 0.01, subtitle, ha="center", va="bottom", fontsize=8)
    fig.tight_layout(rect=(0, 0.04, 1, 0.96))
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return output_path


async def run_load_test(
    *,
    url: str,
    headers: dict[str, str],
    total_requests: int,
    concurrency: int,
    timeout_s: float,
) -> LoadTestReport:
    report = LoadTestReport()
    semaphore = asyncio.Semaphore(concurrency)
    test_started = time.perf_counter()

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(timeout_s)

    async with httpx.AsyncClient(limits=limits, timeout=timeout, follow_redirects=True) as client:
        tasks = [
            one_request(client, url, headers, semaphore, test_started)
            for _ in range(total_requests)
        ]
        for coro in asyncio.as_completed(tasks):
            report.add(await coro)

    report.duration_s = time.perf_counter() - test_started
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Load test auctions suggested endpoint.")
    parser.add_argument(
        "--target",
        choices=sorted(TARGET_URLS),
        default=_env("LOAD_TEST_TARGET") or "",
        help="Preset URL (all use ordering=suggested where applicable): "
        + ", ".join(f"{k}={v}" for k, v in TARGET_URLS.items()),
    )
    parser.add_argument("--url", default=_env("LOAD_TEST_URL") or "")
    parser.add_argument(
        "--authorization",
        default="",
        help="Authorization header (khodro45, bmw). If omitted: khodro45 uses "
        "KHODRO45_AUTHORIZATION or LOAD_TEST_AUTHORIZATION; bmw uses LOAD_TEST_AUTHORIZATION. "
        "Gateway targets never send Authorization unless you pass this explicitly.",
    )
    parser.add_argument(
        "--dealer-id",
        default=_env("LOAD_TEST_DEALER_ID") or "451301",
        help="X-Dealer-Id header (gateway suggested endpoints)",
    )
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--histogram",
        nargs="?",
        const="",
        default="",
        metavar="PATH",
        help="Save latency histogram PNG (default: load_test_results/<endpoint>_latency.png)",
    )
    parser.add_argument("--no-histogram", action="store_true", help="Skip histogram plot")
    parser.add_argument(
        "--timeseries",
        nargs="?",
        const="",
        default="",
        metavar="PATH",
        help="Save time series PNG (default: load_test_results/<endpoint>_timeseries.png)",
    )
    parser.add_argument("--no-timeseries", action="store_true", help="Skip time series plot")
    args = parser.parse_args()

    if args.target:
        url = TARGET_URLS[args.target]
    elif args.url.strip():
        url = args.url.strip()
    else:
        url = DEFAULT_URL

    file_slug = endpoint_file_slug(url)

    headers = dict(DEFAULT_HEADERS)
    auth = resolve_authorization(args.target or "", args.authorization)
    if auth:
        headers["Authorization"] = auth
    dealer_id = args.dealer_id.strip()
    if dealer_id:
        headers["X-Dealer-Id"] = dealer_id

    print("=== Load test config ===")
    print(f"Target:        {args.target or '(custom/default)'}")
    print(f"URL:           {url}")
    print(f"Output slug:   {file_slug}")
    print(f"Requests:      {args.requests}")
    print(f"Concurrency:   {args.concurrency}")
    print(f"Authorization: {'set' if auth else 'NOT SET'}")
    print(f"X-Dealer-Id:   {dealer_id or 'NOT SET'}")
    print_load_pattern(args.requests, args.concurrency)

    report = asyncio.run(
        run_load_test(
            url=url,
            headers=headers,
            total_requests=args.requests,
            concurrency=args.concurrency,
            timeout_s=args.timeout,
        )
    )
    print_report(report)

    if not args.no_histogram:
        hist_path = (
            Path(args.histogram)
            if args.histogram
            else DEFAULT_RESULTS_DIR / f"{file_slug}_latency.png"
        )
        saved = save_latency_histogram(
            report,
            output_path=hist_path,
            url=url,
            concurrency=args.concurrency,
        )
        print(f"\nHistogram saved: {saved}")

    if not args.no_timeseries:
        ts_path = (
            Path(args.timeseries)
            if args.timeseries
            else DEFAULT_RESULTS_DIR / f"{file_slug}_timeseries.png"
        )
        saved_ts = save_time_series_plot(
            report,
            output_path=ts_path,
            url=url,
            concurrency=args.concurrency,
        )
        print(f"Time series saved: {saved_ts}")

    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
