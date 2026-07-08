import concurrent.futures
import json
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen

DEFAULT_ASSET_BASE_URL = "https://" + "aws" + "utils.github.io"
RETRY_ATTEMPTS = 5


def _json_dump(data):
    print(json.dumps(data, indent=4, sort_keys=True))


def _run_command(cmd, *, retries=1):
    last_proc = None
    for attempt in range(1, retries + 1):
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        last_proc = proc
        if proc.returncode == 0:
            return proc
        if attempt < retries:
            time.sleep(min(2 ** (attempt - 1), 8))
    return last_proc


def _aws_call(args):
    proc = _run_command(["aws", *args], retries=RETRY_ATTEMPTS)
    return {"ok": proc.returncode == 0, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}


def _parallel_map(items, worker, max_workers):
    items = list(items)
    if not items:
        return []
    workers = max(1, min(max_workers, len(items)))
    if workers == 1:
        return [worker(item) for item in items]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(worker, item) for item in items]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    return results


def _download_text(url, label):
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            with urlopen(url, timeout=30) as response:
                return response.read().decode("utf-8")
        except URLError as exc:
            last_error = exc
            if attempt < RETRY_ATTEMPTS:
                time.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(f"could not download {label}: {last_error}")


def _create_cloudwatch_dashboard(args):
    base_url = args.base_url.rstrip("/")
    selected = ["full", "simple"] if args.dashboard == "all" else [args.dashboard]

    def create_one(dashboard):
        file_name = f"dashboard_{dashboard}.json"
        dashboard_name = args.name or f"dashboard-{dashboard}"
        if args.name and len(selected) > 1:
            dashboard_name = f"{args.name}-{dashboard}"

        body = _download_text(f"{base_url}/{file_name}", file_name)
        result = _aws_call([
            "cloudwatch",
            "put-dashboard",
            "--dashboard-name",
            dashboard_name,
            "--dashboard-body",
            body,
        ])
        return {
            "dashboard": dashboard,
            "dashboard_name": dashboard_name,
            "source": file_name,
            **result,
        }

    results = _parallel_map(selected, create_one, args.max_workers)

    _json_dump({"dashboards": results})
    return 0 if all(item["ok"] for item in results) else 1


create_cloudwatch_dashboard = _create_cloudwatch_dashboard
