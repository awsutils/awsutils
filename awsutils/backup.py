import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path


INSTALL_ROOT = Path(os.environ.get("AWSUTILS_INSTALL_DIR", Path.home() / ".aws" / "cli" / "tools"))
BACKUP_JOBS_DIR = INSTALL_ROOT / "backup" / "jobs"
PRINT_LOCK = threading.Lock()
RETRY_ATTEMPTS = 5
DEFAULT_SERVICES = (
    "dynamodb",
    "rds",
    "redshift",
    "docdb",
    "elasticache",
    "neptune",
    "ebs",
    "efs",
    "fsx",
)


def _json_dump(data):
    print(json.dumps(data, indent=4, sort_keys=True))


def _public_job_state(state):
    return {key: value for key, value in state.items() if key not in {"stdout", "stderr"}}


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _timestamp():
    return time.strftime("%Y%m%d%H%M%S", time.gmtime())


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


def _aws_text(args, default=""):
    proc = _run_command(["aws", *args, "--output", "text"], retries=RETRY_ATTEMPTS)
    if proc.returncode != 0:
        return default
    text = proc.stdout.strip()
    if text == "None":
        return default
    return text


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


def _print_job_event(message):
    with PRINT_LOCK:
        print(f"[{_utc_now()}] {message}", flush=True)


def _region_arg(region):
    return ["--region", region] if region else []


def _detect_region(region):
    if region:
        return region
    detected = _aws_text(["configure", "get", "region"])
    return detected or os.environ.get("AWS_DEFAULT_REGION", "")


def _backup_job_paths(job_id):
    job_dir = BACKUP_JOBS_DIR / job_id
    return {
        "dir": job_dir,
        "state": job_dir / "job.json",
        "stdout": job_dir / "stdout.log",
        "stderr": job_dir / "stderr.log",
    }


def _parse_services(value):
    if not value:
        return list(DEFAULT_SERVICES)
    aliases = {"all": DEFAULT_SERVICES, "cache": ("elasticache",), "elasticache": ("elasticache",)}
    services = []
    for item in value.split(","):
        name = item.strip().lower()
        if not name:
            continue
        expanded = aliases.get(name, (name,))
        for service in expanded:
            if service not in DEFAULT_SERVICES:
                raise ValueError(f"unsupported backup service '{service}'")
            if service not in services:
                services.append(service)
    return services


def _safe_name(prefix, service, resource_id, max_len=63):
    text = f"{prefix}-{_timestamp()}-{service}-{resource_id}".lower()
    text = re.sub(r"[^a-z0-9-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    if not text or not text[0].isalpha():
        text = f"b-{text}"
    return text[:max_len].rstrip("-")


def _list_dynamodb(region):
    names = []
    start = None
    while True:
        args = ["dynamodb", "list-tables", *_region_arg(region)]
        if start:
            args.extend(["--exclusive-start-table-name", start])
        data = _aws_json(args, default={}) or {}
        names.extend(data.get("TableNames", []))
        start = data.get("LastEvaluatedTableName")
        if not start:
            return [{"service": "dynamodb", "id": name} for name in names]


def _list_rds(region):
    resources = []
    data = _aws_json(["rds", "describe-db-instances", *_region_arg(region)], default={}) or {}
    for item in data.get("DBInstances", []):
        identifier = item.get("DBInstanceIdentifier")
        if identifier:
            resources.append({"service": "rds", "type": "instance", "id": identifier})
    data = _aws_json(["rds", "describe-db-clusters", *_region_arg(region)], default={}) or {}
    for item in data.get("DBClusters", []):
        identifier = item.get("DBClusterIdentifier")
        engine = item.get("Engine", "")
        if identifier and engine not in {"docdb", "neptune"}:
            resources.append({"service": "rds", "type": "cluster", "id": identifier})
    return resources


def _list_redshift(region):
    data = _aws_json(["redshift", "describe-clusters", *_region_arg(region)], default={}) or {}
    return [{"service": "redshift", "id": item["ClusterIdentifier"]} for item in data.get("Clusters", []) if item.get("ClusterIdentifier")]


def _list_docdb(region):
    data = _aws_json(["docdb", "describe-db-clusters", *_region_arg(region)], default={}) or {}
    return [{"service": "docdb", "id": item["DBClusterIdentifier"]} for item in data.get("DBClusters", []) if item.get("DBClusterIdentifier")]


def _list_elasticache(region):
    resources = []
    data = _aws_json(["elasticache", "describe-replication-groups", *_region_arg(region)], default={}) or {}
    replication_groups = {item.get("ReplicationGroupId") for item in data.get("ReplicationGroups", []) if item.get("ReplicationGroupId")}
    resources.extend({"service": "elasticache", "type": "replication-group", "id": item} for item in sorted(replication_groups))
    data = _aws_json(["elasticache", "describe-cache-clusters", *_region_arg(region)], default={}) or {}
    for item in data.get("CacheClusters", []):
        cluster_id = item.get("CacheClusterId")
        if cluster_id and not item.get("ReplicationGroupId"):
            resources.append({"service": "elasticache", "type": "cache-cluster", "id": cluster_id})
    return resources


def _list_neptune(region):
    data = _aws_json(["neptune", "describe-db-clusters", *_region_arg(region)], default={}) or {}
    return [{"service": "neptune", "id": item["DBClusterIdentifier"]} for item in data.get("DBClusters", []) if item.get("DBClusterIdentifier")]


def _list_ebs(region):
    data = _aws_json(["ec2", "describe-volumes", *_region_arg(region)], default={}) or {}
    return [{"service": "ebs", "id": item["VolumeId"]} for item in data.get("Volumes", []) if item.get("VolumeId")]


def _list_efs(region):
    data = _aws_json(["efs", "describe-file-systems", *_region_arg(region)], default={}) or {}
    return [{"service": "efs", "id": item["FileSystemId"]} for item in data.get("FileSystems", []) if item.get("FileSystemId")]


def _list_fsx(region):
    data = _aws_json(["fsx", "describe-file-systems", *_region_arg(region)], default={}) or {}
    return [{"service": "fsx", "id": item["FileSystemId"]} for item in data.get("FileSystems", []) if item.get("FileSystemId")]


def _discover_resources(services, region):
    listers = {
        "dynamodb": _list_dynamodb,
        "rds": _list_rds,
        "redshift": _list_redshift,
        "docdb": _list_docdb,
        "elasticache": _list_elasticache,
        "neptune": _list_neptune,
        "ebs": _list_ebs,
        "efs": _list_efs,
        "fsx": _list_fsx,
    }
    resources = []
    for service in services:
        discovered = listers[service](region)
        _print_job_event(f"{service}: discovered {len(discovered)} resources")
        resources.extend(discovered)
    return resources


def _create_resource_backup(resource, region, prefix):
    service = resource["service"]
    resource_id = resource["id"]
    name = _safe_name(prefix, service, resource_id, max_len=40 if service == "elasticache" else 63)

    if service == "dynamodb":
        result = _aws_call(["dynamodb", "create-backup", *_region_arg(region), "--table-name", resource_id, "--backup-name", name])
    elif service == "rds" and resource.get("type") == "cluster":
        result = _aws_call(["rds", "create-db-cluster-snapshot", *_region_arg(region), "--db-cluster-identifier", resource_id, "--db-cluster-snapshot-identifier", name])
    elif service == "rds":
        result = _aws_call(["rds", "create-db-snapshot", *_region_arg(region), "--db-instance-identifier", resource_id, "--db-snapshot-identifier", name])
    elif service == "redshift":
        result = _aws_call(["redshift", "create-cluster-snapshot", *_region_arg(region), "--cluster-identifier", resource_id, "--snapshot-identifier", name])
    elif service == "docdb":
        result = _aws_call(["docdb", "create-db-cluster-snapshot", *_region_arg(region), "--db-cluster-identifier", resource_id, "--db-cluster-snapshot-identifier", name])
    elif service == "elasticache" and resource.get("type") == "replication-group":
        result = _aws_call(["elasticache", "create-snapshot", *_region_arg(region), "--replication-group-id", resource_id, "--snapshot-name", name])
    elif service == "elasticache":
        result = _aws_call(["elasticache", "create-snapshot", *_region_arg(region), "--cache-cluster-id", resource_id, "--snapshot-name", name])
    elif service == "neptune":
        result = _aws_call(["neptune", "create-db-cluster-snapshot", *_region_arg(region), "--db-cluster-identifier", resource_id, "--db-cluster-snapshot-identifier", name])
    elif service == "ebs":
        result = _aws_call(["ec2", "create-snapshot", *_region_arg(region), "--volume-id", resource_id, "--description", name])
    elif service == "efs":
        result = _aws_call(["efs", "create-backup", *_region_arg(region), "--file-system-id", resource_id])
    elif service == "fsx":
        result = _aws_call(["fsx", "create-backup", *_region_arg(region), "--file-system-id", resource_id])
    else:
        result = {"ok": False, "stdout": "", "stderr": f"unsupported resource service {service}"}

    _print_job_event(f"{service}:{resource_id}: {'created' if result['ok'] else 'failed'} backup {name}")
    return {"service": service, "resource_id": resource_id, "backup_name": name, **result}


def _create_backup_job(args):
    services = _parse_services(args.services)
    job_id = str(uuid.uuid4())
    paths = _backup_job_paths(job_id)
    paths["dir"].mkdir(parents=True, exist_ok=True)

    runner_args = ["--services", ",".join(services), "--prefix", args.prefix, "--max-workers", str(args.max_workers)]
    if args.region:
        runner_args.extend(["--region", args.region])

    state = {
        "job_id": job_id,
        "status": "PENDING",
        "created_at": _utc_now(),
        "stdout": str(paths["stdout"]),
        "stderr": str(paths["stderr"]),
        "region": args.region,
        "services": services,
        "prefix": args.prefix,
        "max_workers": args.max_workers,
    }
    _write_json(paths["state"], state)

    cmd = [sys.executable, "-m", "awsutils.cli", "_run-backup-job", job_id, *runner_args]
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
    _json_dump(_public_job_state(state))
    return 0


def _describe_backup_job(args):
    if not args.job_id:
        jobs = []
        if BACKUP_JOBS_DIR.exists():
            for state_path in sorted(BACKUP_JOBS_DIR.glob("*/job.json")):
                state = _read_json(state_path)
                state["stdout_bytes"] = len(_read_clean_text(state_path.parent / "stdout.log").encode("utf-8"))
                state["stderr_bytes"] = len(_read_clean_text(state_path.parent / "stderr.log").encode("utf-8"))
                jobs.append(_public_job_state(state))
        _json_dump({"jobs": jobs})
        return 0

    paths = _backup_job_paths(args.job_id)
    if not paths["state"].exists():
        _json_dump({"job_id": args.job_id, "status": "NOT_FOUND"})
        return 1
    state = _read_json(paths["state"])
    state["stdout_text"] = _read_clean_text(paths["stdout"])
    state["stderr_text"] = _read_clean_text(paths["stderr"])
    _json_dump(_public_job_state(state))
    return 0


def _run_backup_job(args):
    paths = _backup_job_paths(args.job_id)
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
            services = _parse_services(args.services)
            resources = _discover_resources(services, region)
            results = _parallel_map(resources, lambda resource: _create_resource_backup(resource, region, args.prefix), args.max_workers)
            failed = [result for result in results if not result.get("ok")]
            state["status"] = "SUCCEEDED" if not failed else "FAILED"
            state["completed_at"] = _utc_now()
            state["region"] = region
            state["services"] = services
            state["resource_count"] = len(resources)
            state["backup_count"] = len(results) - len(failed)
            state["failed_count"] = len(failed)
            state["results"] = results
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


create_backup_job = _create_backup_job
describe_backup_job = _describe_backup_job
run_backup_job = _run_backup_job
