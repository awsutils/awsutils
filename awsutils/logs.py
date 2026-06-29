import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path


INSTALL_ROOT = Path(os.environ.get("AWSUTILS_INSTALL_DIR", Path.home() / ".awsutils"))
LOGS_JOBS_DIR = INSTALL_ROOT / "logs" / "jobs"
PRINT_LOCK = threading.Lock()
RETRY_ATTEMPTS = 5
DEFAULT_RETENTION_DAYS = 7
DEFAULT_TAGS = {
    "ManagedBy": "awsutils",
}


def _json_dump(data):
    print(json.dumps(data, indent=4, sort_keys=True))


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=4, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _read_clean_text(path):
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


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


def _aws_json(args, default=None):
    proc = _run_command(["aws", *args, "--output", "json"], retries=RETRY_ATTEMPTS)
    if proc.returncode != 0:
        return default
    text = proc.stdout.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _aws_ok(args):
    return _run_command(["aws", *args], retries=RETRY_ATTEMPTS).returncode == 0


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


def _print_job_event(message):
    with PRINT_LOCK:
        print(f"[{_utc_now()}] {message}", flush=True)


def _region_arg(region):
    return ["--region", region] if region else []


def _detect_region(region):
    if region:
        return region
    proc = _run_command(["aws", "configure", "get", "region"], retries=RETRY_ATTEMPTS)
    detected = proc.stdout.strip() if proc.returncode == 0 else ""
    return detected or os.environ.get("AWS_DEFAULT_REGION", "")


def _logs_job_paths(job_id):
    job_dir = LOGS_JOBS_DIR / job_id
    return {
        "dir": job_dir,
        "state": job_dir / "job.json",
        "stdout": job_dir / "stdout.log",
        "stderr": job_dir / "stderr.log",
    }


def _parse_tags(tag_args):
    tags = dict(DEFAULT_TAGS)
    for item in tag_args or []:
        if "=" not in item:
            raise ValueError(f"invalid tag '{item}'; expected Key=Value")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"invalid tag '{item}'; tag key is empty")
        tags[key] = value.strip()
    return tags


def _list_log_groups(region):
    groups = []
    next_token = None
    while True:
        args = ["logs", "describe-log-groups", *_region_arg(region)]
        if next_token:
            args.extend(["--next-token", next_token])
        data = _aws_json(args, default={}) or {}
        groups.extend(data.get("logGroups", []))
        next_token = data.get("nextToken")
        if not next_token:
            return groups


def _fix_log_group(group, region, retention_days, tags):
    name = group.get("logGroupName")
    arn = group.get("logGroupArn") or group.get("arn")
    if not name:
        return {"ok": False, "error": "missing logGroupName"}

    retention_ok = _aws_ok([
        "logs",
        "put-retention-policy",
        *_region_arg(region),
        "--log-group-name",
        name,
        "--retention-in-days",
        str(retention_days),
    ])

    deletion_protection_ok = _aws_ok([
        "logs",
        "put-log-group-deletion-protection",
        *_region_arg(region),
        "--log-group-identifier",
        arn.removesuffix(":*") if arn else name,
        "--deletion-protection-enabled",
    ])

    tag_ok = False
    if arn:
        clean_arn = arn.removesuffix(":*")
        tag_args = [f"{key}={value}" for key, value in tags.items()]
        tag_ok = _aws_ok(["logs", "tag-resource", *_region_arg(region), "--resource-arn", clean_arn, "--tags", *tag_args])
    if not tag_ok:
        tag_args = [f"{key}={value}" for key, value in tags.items()]
        tag_ok = _aws_ok(["logs", "tag-log-group", *_region_arg(region), "--log-group-name", name, "--tags", *tag_args])

    ok = retention_ok and deletion_protection_ok and tag_ok
    _print_job_event(
        f"{name}: {'fixed' if ok else 'failed'} "
        f"retention={retention_ok} deletion_protection={deletion_protection_ok} tags={tag_ok}"
    )
    return {
        "log_group_name": name,
        "ok": ok,
        "retention_ok": retention_ok,
        "deletion_protection_ok": deletion_protection_ok,
        "tag_ok": tag_ok,
    }


def _create_logs_fix_job(args):
    tags = _parse_tags(args.tag)
    job_id = str(uuid.uuid4())
    paths = _logs_job_paths(job_id)
    paths["dir"].mkdir(parents=True, exist_ok=True)

    runner_args = ["--retention-days", str(args.retention_days), "--max-workers", str(args.max_workers)]
    if args.region:
        runner_args.extend(["--region", args.region])
    for key, value in tags.items():
        runner_args.extend(["--tag", f"{key}={value}"])

    state = {
        "job_id": job_id,
        "status": "PENDING",
        "created_at": _utc_now(),
        "stdout": str(paths["stdout"]),
        "stderr": str(paths["stderr"]),
        "region": args.region,
        "retention_days": args.retention_days,
        "tags": tags,
        "max_workers": args.max_workers,
    }
    _write_json(paths["state"], state)

    cmd = [sys.executable, "-m", "awsutils.cli", "_run-logs-fix-job", job_id, *runner_args]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    state["status"] = "RUNNING"
    state["pid"] = proc.pid
    state["started_at"] = _utc_now()
    _write_json(paths["state"], state)
    _json_dump(state)
    return 0


def _describe_logs_fix_job(args):
    if not args.job_id:
        jobs = []
        if LOGS_JOBS_DIR.exists():
            for state_path in sorted(LOGS_JOBS_DIR.glob("*/job.json")):
                state = _read_json(state_path)
                state["stdout_bytes"] = len(_read_clean_text(state_path.parent / "stdout.log").encode("utf-8"))
                state["stderr_bytes"] = len(_read_clean_text(state_path.parent / "stderr.log").encode("utf-8"))
                jobs.append(state)
        _json_dump({"jobs": jobs})
        return 0

    paths = _logs_job_paths(args.job_id)
    if not paths["state"].exists():
        _json_dump({"job_id": args.job_id, "status": "NOT_FOUND"})
        return 1
    state = _read_json(paths["state"])
    state["stdout_text"] = _read_clean_text(paths["stdout"])
    state["stderr_text"] = _read_clean_text(paths["stderr"])
    _json_dump(state)
    return 0


def _run_logs_fix_job(args):
    paths = _logs_job_paths(args.job_id)
    state = _read_json(paths["state"])
    state["status"] = "RUNNING"
    state["runner_pid"] = os.getpid()
    state.setdefault("started_at", _utc_now())
    _write_json(paths["state"], state)

    with paths["stdout"].open("w", encoding="utf-8") as stdout, paths["stderr"].open("w", encoding="utf-8") as stderr:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout, stderr
        try:
            region = _detect_region(args.region)
            if not region:
                raise RuntimeError("could not determine AWS region")
            tags = _parse_tags(args.tag)
            groups = _list_log_groups(region)
            results = _parallel_map(groups, lambda group: _fix_log_group(group, region, args.retention_days, tags), args.max_workers)
            failed = [result for result in results if not result.get("ok")]
            state["status"] = "SUCCEEDED" if not failed else "FAILED"
            state["completed_at"] = _utc_now()
            state["region"] = region
            state["log_group_count"] = len(groups)
            state["failed_count"] = len(failed)
            state["retention_days"] = args.retention_days
            state["tags"] = tags
            _write_json(paths["state"], state)
            return 0 if not failed else 1
        except Exception as exc:
            print(str(exc), file=sys.stderr, flush=True)
            state["status"] = "FAILED"
            state["completed_at"] = _utc_now()
            state["error"] = str(exc)
            _write_json(paths["state"], state)
            return 1
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr


create_logs_fix_job = _create_logs_fix_job
describe_logs_fix_job = _describe_logs_fix_job
run_logs_fix_job = _run_logs_fix_job
