import concurrent.futures
import ipaddress
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from awsutils.s3 import ensure_log_bucket as ensure_s3_log_bucket

INSTALL_ROOT = Path(os.environ.get("AWSUTILS_INSTALL_DIR", Path.home() / ".aws" / "cli" / "tools"))
VPC_JOBS_DIR = INSTALL_ROOT / "vpc" / "jobs"
GATEWAY_ENDPOINTS = ("s3", "dynamodb")
INTERFACE_ENDPOINTS = ("ecr.dkr", "ecr.api", "ssm", "ssmmessages", "ec2messages", "sqs", "sns")
PRINT_LOCK = threading.Lock()
RETRY_ATTEMPTS = 5


def _json_dump(data):
    print(json.dumps(data, indent=4, sort_keys=True))


def _public_job_state(state):
    return {key: value for key, value in state.items() if key not in {"stdout", "stderr"}}


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


def _aws_text(args, default=""):
    proc = _run_command(["aws", *args, "--output", "text"], retries=RETRY_ATTEMPTS)
    if proc.returncode != 0:
        return default
    text = proc.stdout.strip()
    if text == "None":
        return default
    return text


def _aws_ok(args):
    return _run_command(["aws", *args], retries=RETRY_ATTEMPTS).returncode == 0


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


def _vpc_job_paths(job_id):
    job_dir = VPC_JOBS_DIR / job_id
    return {
        "dir": job_dir,
        "state": job_dir / "job.json",
        "stdout": job_dir / "stdout.log",
        "stderr": job_dir / "stderr.log",
    }


def _create_vpc_fix_job(args):
    job_id = str(uuid.uuid4())
    paths = _vpc_job_paths(job_id)
    paths["dir"].mkdir(parents=True, exist_ok=True)

    runner_args = []
    if args.vpc_ids:
        runner_args.extend(["--vpc-ids", args.vpc_ids])
    if args.region:
        runner_args.extend(["--region", args.region])
    runner_args.extend(["--max-workers", str(args.max_workers)])

    state = {
        "job_id": job_id,
        "status": "PENDING",
        "created_at": _utc_now(),
        "stdout": str(paths["stdout"]),
        "stderr": str(paths["stderr"]),
        "vpc_ids": args.vpc_ids,
        "region": args.region,
        "max_workers": args.max_workers,
    }
    _write_json(paths["state"], state)

    cmd = [sys.executable, "-m", "awsutils.cli", "_run-vpc-fix-job", job_id, *runner_args]
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


def _describe_vpc_fix_job(args):
    if not args.job_id:
        jobs = []
        if VPC_JOBS_DIR.exists():
            for state_path in sorted(VPC_JOBS_DIR.glob("*/job.json")):
                state = _read_json(state_path)
                state["stdout_bytes"] = len(_read_clean_text(state_path.parent / "stdout.log").encode("utf-8"))
                state["stderr_bytes"] = len(_read_clean_text(state_path.parent / "stderr.log").encode("utf-8"))
                jobs.append(_public_job_state(state))
        _json_dump({"jobs": jobs})
        return 0

    paths = _vpc_job_paths(args.job_id)
    if not paths["state"].exists():
        _json_dump({"job_id": args.job_id, "status": "NOT_FOUND"})
        return 1
    state = _read_json(paths["state"])
    state["stdout_text"] = _read_clean_text(paths["stdout"])
    state["stderr_text"] = _read_clean_text(paths["stderr"])
    _json_dump(_public_job_state(state))
    return 0


def _print_job_event(message):
    with PRINT_LOCK:
        print(f"[{_utc_now()}] {message}", flush=True)


def _region_arg(region):
    return ["--region", region] if region else []


def _detect_region(region):
    if region:
        return region
    detected = _aws_text(["ec2", "describe-availability-zones", "--query", "AvailabilityZones[0].RegionName"])
    if detected:
        return detected
    detected = _aws_text(["configure", "get", "region"])
    return detected or os.environ.get("AWS_DEFAULT_REGION", "")


def _vpc_name(vpc_id, region):
    data = _aws_json([
        "ec2",
        "describe-vpcs",
        *_region_arg(region),
        "--vpc-ids",
        vpc_id,
        "--query",
        "Vpcs[0]",
    ], default={}) or {}
    for tag in data.get("Tags", []):
        if tag.get("Key") == "Name" and tag.get("Value"):
            return tag["Value"]
    return vpc_id


def _vpc_cidr(vpc_id, region):
    return _aws_text(["ec2", "describe-vpcs", *_region_arg(region), "--vpc-ids", vpc_id, "--query", "Vpcs[0].CidrBlock"])


def _all_vpc_ids(region):
    text = _aws_text(["ec2", "describe-vpcs", *_region_arg(region), "--query", "Vpcs[*].VpcId"])
    return text.split() if text else []


def _availability_zones(region):
    return _aws_json([
        "ec2",
        "describe-availability-zones",
        *_region_arg(region),
        "--filters",
        "Name=state,Values=available",
        "--query",
        "AvailabilityZones[*].ZoneName",
    ], default=[]) or []


def _subnets(vpc_id, region):
    return _aws_json([
        "ec2",
        "describe-subnets",
        *_region_arg(region),
        "--filters",
        f"Name=vpc-id,Values={vpc_id}",
        "--query",
        "Subnets",
    ], default=[]) or []


def _route_tables(vpc_id, region):
    return _aws_json([
        "ec2",
        "describe-route-tables",
        *_region_arg(region),
        "--filters",
        f"Name=vpc-id,Values={vpc_id}",
        "--query",
        "RouteTables",
    ], default=[]) or []


def _all_route_table_ids(vpc_id, region):
    return [table.get("RouteTableId") for table in _route_tables(vpc_id, region) if table.get("RouteTableId")]


def _route_table_has_igw(table):
    for route in table.get("Routes", []):
        if route.get("DestinationCidrBlock") == "0.0.0.0/0" and str(route.get("GatewayId", "")).startswith("igw-"):
            return True
    return False


def _main_route_table(tables):
    return next((table for table in tables if any(assoc.get("Main") for assoc in table.get("Associations", []))), None)


def _explicit_route_table_map(tables):
    out = {}
    for table in tables:
        for assoc in table.get("Associations", []):
            subnet_id = assoc.get("SubnetId")
            if subnet_id:
                out[subnet_id] = table
    return out


def _effective_route_table(subnet_id, tables):
    return _explicit_route_table_map(tables).get(subnet_id) or _main_route_table(tables)


def _subnet_ids_by_route(vpc_id, region, public):
    tables = _route_tables(vpc_id, region)
    result = []
    for subnet in _subnets(vpc_id, region):
        table = _effective_route_table(subnet.get("SubnetId"), tables)
        if table and _route_table_has_igw(table) == public:
            result.append(subnet.get("SubnetId"))
    return [item for item in result if item]


def _subnets_in_az(subnet_ids, subnet_by_id, az):
    return [subnet_id for subnet_id in subnet_ids if subnet_by_id.get(subnet_id, {}).get("AvailabilityZone") == az]


def _has_subnets_in_each_az(subnet_ids, subnet_by_id, azs):
    return all(_subnets_in_az(subnet_ids, subnet_by_id, az) for az in azs)


def _next_free_subnet_cidrs(vpc_cidr, existing_cidrs, count):
    if count <= 0:
        return []
    vpc_network = ipaddress.ip_network(vpc_cidr)
    existing = [ipaddress.ip_network(cidr) for cidr in existing_cidrs]
    start_prefix = 24 if vpc_network.prefixlen < 24 else vpc_network.prefixlen + 1
    for prefix in range(start_prefix, 29):
        chosen = []
        for candidate in vpc_network.subnets(new_prefix=prefix):
            if any(candidate.overlaps(used) for used in [*existing, *chosen]):
                continue
            chosen.append(candidate)
            if len(chosen) == count:
                return [str(cidr) for cidr in chosen]
    return []


def _ensure_igw(vpc_id, vpc_name, region):
    igw_id = _aws_text([
        "ec2",
        "describe-internet-gateways",
        *_region_arg(region),
        "--filters",
        f"Name=attachment.vpc-id,Values={vpc_id}",
        "--query",
        "InternetGateways[0].InternetGatewayId",
    ])
    if igw_id:
        return igw_id
    igw_id = _aws_text([
        "ec2",
        "create-internet-gateway",
        *_region_arg(region),
        "--tag-specifications",
        f"ResourceType=internet-gateway,Tags=[{{Key=Name,Value={vpc_name}-igw}}]",
        "--query",
        "InternetGateway.InternetGatewayId",
    ])
    if not igw_id:
        return ""
    if not _aws_ok(["ec2", "attach-internet-gateway", *_region_arg(region), "--internet-gateway-id", igw_id, "--vpc-id", vpc_id]):
        return ""
    _print_job_event(f"{vpc_id}: created internet gateway {igw_id}")
    return igw_id


def _vpc_dns_enabled(vpc_id, region, attribute):
    attr_key = "EnableDnsSupport" if attribute == "enableDnsSupport" else "EnableDnsHostnames"
    value = _aws_text([
        "ec2",
        "describe-vpc-attribute",
        *_region_arg(region),
        "--vpc-id",
        vpc_id,
        "--attribute",
        attribute,
        "--query",
        f"{attr_key}.Value",
    ])
    return value == "True"


def _ensure_vpc_dns_attributes(vpc_id, region):
    dns_support = _vpc_dns_enabled(vpc_id, region, "enableDnsSupport")
    if not dns_support:
        dns_support = _aws_ok([
            "ec2",
            "modify-vpc-attribute",
            *_region_arg(region),
            "--vpc-id",
            vpc_id,
            "--enable-dns-support",
        ])
    dns_hostnames = _vpc_dns_enabled(vpc_id, region, "enableDnsHostnames")
    if not dns_hostnames:
        dns_hostnames = _aws_ok([
            "ec2",
            "modify-vpc-attribute",
            *_region_arg(region),
            "--vpc-id",
            vpc_id,
            "--enable-dns-hostnames",
        ])

    if not dns_support:
        _print_job_event(f"{vpc_id}: failed to enable DNS support")
    if not dns_hostnames:
        _print_job_event(f"{vpc_id}: failed to enable DNS hostnames")
    return dns_support and dns_hostnames


def _create_subnet(vpc_id, vpc_name, az, cidr, tier, region):
    subnet_id = _aws_text([
        "ec2",
        "create-subnet",
        *_region_arg(region),
        "--vpc-id",
        vpc_id,
        "--availability-zone",
        az,
        "--cidr-block",
        cidr,
        "--tag-specifications",
        f"ResourceType=subnet,Tags=[{{Key=Name,Value={vpc_name}-{tier}-{az}}},{{Key=Tier,Value={tier}}}]",
        "--query",
        "Subnet.SubnetId",
    ])
    if subnet_id and tier == "public":
        _aws_ok(["ec2", "modify-subnet-attribute", *_region_arg(region), "--subnet-id", subnet_id, "--map-public-ip-on-launch"])
    if subnet_id:
        _print_job_event(f"{vpc_id}: created {tier} subnet {subnet_id} ({cidr}, {az})")
    return subnet_id


def _explicit_route_table_id(subnet_id, region):
    return _aws_text([
        "ec2",
        "describe-route-tables",
        *_region_arg(region),
        "--filters",
        f"Name=association.subnet-id,Values={subnet_id}",
        "--query",
        "RouteTables[0].RouteTableId",
    ])


def _ensure_public_route_table(vpc_id, vpc_name, igw_id, public_subnet_ids, region):
    rt_id = _aws_text([
        "ec2",
        "describe-route-tables",
        *_region_arg(region),
        "--filters",
        f"Name=vpc-id,Values={vpc_id}",
        "--query",
        "RouteTables[?Routes[?DestinationCidrBlock=='0.0.0.0/0' && GatewayId!=null && starts_with(GatewayId,'igw-')]].RouteTableId | [0]",
    ])
    if not rt_id:
        rt_id = _aws_text([
            "ec2",
            "create-route-table",
            *_region_arg(region),
            "--vpc-id",
            vpc_id,
            "--tag-specifications",
            f"ResourceType=route-table,Tags=[{{Key=Name,Value={vpc_name}-public-rt}}]",
            "--query",
            "RouteTable.RouteTableId",
        ])
        if not rt_id:
            return ""
        _aws_ok(["ec2", "create-route", *_region_arg(region), "--route-table-id", rt_id, "--destination-cidr-block", "0.0.0.0/0", "--gateway-id", igw_id])
        _print_job_event(f"{vpc_id}: created public route table {rt_id}")
    for subnet_id in public_subnet_ids:
        if not _explicit_route_table_id(subnet_id, region):
            _aws_ok(["ec2", "associate-route-table", *_region_arg(region), "--route-table-id", rt_id, "--subnet-id", subnet_id])
    return rt_id


def _ensure_nat_gateway(vpc_id, vpc_name, public_subnet_id, az, region):
    natgw_id = _aws_text([
        "ec2",
        "describe-nat-gateways",
        *_region_arg(region),
        "--filter",
        f"Name=vpc-id,Values={vpc_id}",
        f"Name=subnet-id,Values={public_subnet_id}",
        "Name=state,Values=available,pending",
        "--query",
        "NatGateways[0].NatGatewayId",
    ])
    if natgw_id:
        _aws_ok(["ec2", "wait", "nat-gateway-available", *_region_arg(region), "--nat-gateway-ids", natgw_id])
        return natgw_id
    alloc_id = _aws_text([
        "ec2",
        "allocate-address",
        *_region_arg(region),
        "--domain",
        "vpc",
        "--tag-specifications",
        f"ResourceType=elastic-ip,Tags=[{{Key=Name,Value={vpc_name}-nat-eip-{az}}}]",
        "--query",
        "AllocationId",
    ])
    if not alloc_id:
        return ""
    natgw_id = _aws_text([
        "ec2",
        "create-nat-gateway",
        *_region_arg(region),
        "--subnet-id",
        public_subnet_id,
        "--allocation-id",
        alloc_id,
        "--tag-specifications",
        f"ResourceType=natgateway,Tags=[{{Key=Name,Value={vpc_name}-natgw-{az}}}]",
        "--query",
        "NatGateway.NatGatewayId",
    ])
    if not natgw_id:
        return ""
    _print_job_event(f"{vpc_id}: creating NAT gateway {natgw_id} in {az}")
    if not _aws_ok(["ec2", "wait", "nat-gateway-available", *_region_arg(region), "--nat-gateway-ids", natgw_id]):
        return ""
    _print_job_event(f"{vpc_id}: NAT gateway available {natgw_id} in {az}")
    return natgw_id


def _ensure_private_routes(vpc_id, vpc_name, natgw_id, private_subnet_ids, az, region):
    if not private_subnet_ids:
        return

    desired_name = f"{vpc_name}-private-rt-{az}"
    rt_id = _aws_text([
        "ec2",
        "describe-route-tables",
        *_region_arg(region),
        "--filters",
        f"Name=vpc-id,Values={vpc_id}",
        f"Name=tag:Name,Values={desired_name}",
        "--query",
        "RouteTables[0].RouteTableId",
    ])

    rt_data = {}
    if rt_id:
        rt_data = _aws_json([
            "ec2",
            "describe-route-tables",
            *_region_arg(region),
            "--route-table-ids",
            rt_id,
            "--query",
            "RouteTables[0]",
        ], default={}) or {}
        default_route = next((route for route in rt_data.get("Routes", []) if route.get("DestinationCidrBlock") == "0.0.0.0/0"), None)
        if _route_table_has_igw(rt_data) or not default_route or default_route.get("NatGatewayId") != natgw_id:
            rt_id = ""

    if not rt_id:
        rt_id = _aws_text([
            "ec2",
            "create-route-table",
            *_region_arg(region),
            "--vpc-id",
            vpc_id,
            "--tag-specifications",
            f"ResourceType=route-table,Tags=[{{Key=Name,Value={desired_name}}}]",
            "--query",
            "RouteTable.RouteTableId",
        ])
        if not rt_id:
            return
        _print_job_event(f"{vpc_id}: created private route table {rt_id} for {az}")

    current_rt_ids = {subnet_id: _explicit_route_table_id(subnet_id, region) for subnet_id in private_subnet_ids}
    for subnet_id in private_subnet_ids:
        if current_rt_ids.get(subnet_id) != rt_id:
            _aws_ok(["ec2", "associate-route-table", *_region_arg(region), "--route-table-id", rt_id, "--subnet-id", subnet_id])
            _print_job_event(f"{vpc_id}: associated private route table {rt_id} with {subnet_id}")

    rt_data = _aws_json(["ec2", "describe-route-tables", *_region_arg(region), "--route-table-ids", rt_id, "--query", "RouteTables[0]"], default={}) or {}
    default_route = next((route for route in rt_data.get("Routes", []) if route.get("DestinationCidrBlock") == "0.0.0.0/0"), None)
    if default_route and default_route.get("NatGatewayId") == natgw_id:
        return
    action = "replace-route" if default_route else "create-route"
    if _aws_ok(["ec2", action, *_region_arg(region), "--route-table-id", rt_id, "--destination-cidr-block", "0.0.0.0/0", "--nat-gateway-id", natgw_id]):
        _print_job_event(f"{vpc_id}: {'replaced' if default_route else 'added'} private default route on {rt_id} for {az}")


def _fix_vpc_networking(vpc_id, vpc_name, vpc_cidr, region, max_workers=8):
    igw_id = _ensure_igw(vpc_id, vpc_name, region)
    if not igw_id:
        _print_job_event(f"{vpc_id}: could not ensure internet gateway; skipping network repair")
        return

    target_azs = _availability_zones(region)[:2]
    if len(target_azs) < 2:
        _print_job_event(f"{vpc_id}: at least two availability zones are required for network repair")
        return

    all_subnets = _subnets(vpc_id, region)
    subnet_by_id = {subnet.get("SubnetId"): subnet for subnet in all_subnets if subnet.get("SubnetId")}
    public_subnet_ids = _subnet_ids_by_route(vpc_id, region, public=True)
    private_subnet_ids = _subnet_ids_by_route(vpc_id, region, public=False)

    missing = []
    for tier, subnet_ids in (("public", public_subnet_ids), ("private", private_subnet_ids)):
        for az in target_azs:
            if not _subnets_in_az(subnet_ids, subnet_by_id, az):
                missing.append((tier, az))

    cidrs = _next_free_subnet_cidrs(vpc_cidr, [subnet.get("CidrBlock") for subnet in all_subnets if subnet.get("CidrBlock")], len(missing))
    for (tier, az), cidr in zip(missing, cidrs):
        subnet_id = _create_subnet(vpc_id, vpc_name, az, cidr, tier, region)
        if not subnet_id:
            continue
        subnet_by_id[subnet_id] = {"SubnetId": subnet_id, "AvailabilityZone": az, "CidrBlock": cidr}
        if tier == "public":
            public_subnet_ids.append(subnet_id)
        else:
            private_subnet_ids.append(subnet_id)
    if len(cidrs) < len(missing):
        _print_job_event(f"{vpc_id}: no free CIDR available for {len(missing) - len(cidrs)} missing subnets")

    _ensure_public_route_table(vpc_id, vpc_name, igw_id, public_subnet_ids, region)
    public_subnet_ids = _subnet_ids_by_route(vpc_id, region, public=True)
    private_subnet_ids = _subnet_ids_by_route(vpc_id, region, public=False)
    subnet_by_id = {subnet.get("SubnetId"): subnet for subnet in _subnets(vpc_id, region) if subnet.get("SubnetId")}

    if not _has_subnets_in_each_az(public_subnet_ids, subnet_by_id, target_azs) or not _has_subnets_in_each_az(private_subnet_ids, subnet_by_id, target_azs):
        _print_job_event(f"{vpc_id}: at least two public and two private subnets are required; skipping NAT route repair")
        return

    def _fix_az_nat_and_routes(az):
        public_in_az = _subnets_in_az(public_subnet_ids, subnet_by_id, az)
        private_in_az = _subnets_in_az(private_subnet_ids, subnet_by_id, az)
        if not public_in_az or not private_in_az:
            _print_job_event(f"{vpc_id}: missing public or private subnet in {az}; skipping NAT route repair for AZ")
            return False
        natgw_id = _ensure_nat_gateway(vpc_id, vpc_name, public_in_az[0], az, region)
        if not natgw_id:
            _print_job_event(f"{vpc_id}: could not ensure NAT gateway in {az}; skipping private route repair for AZ")
            return False
        _ensure_private_routes(vpc_id, vpc_name, natgw_id, private_in_az, az, region)
        return True

    _parallel_map(target_azs, _fix_az_nat_and_routes, max_workers=max(1, min(2, max_workers)))


def _private_subnet_ids(vpc_id, region):
    tables = _route_tables(vpc_id, region)
    main_table = next((table for table in tables if any(assoc.get("Main") for assoc in table.get("Associations", []))), None)
    explicit = {}
    for table in tables:
        for assoc in table.get("Associations", []):
            subnet_id = assoc.get("SubnetId")
            if subnet_id:
                explicit[subnet_id] = table
    subnets = _aws_json([
        "ec2",
        "describe-subnets",
        *_region_arg(region),
        "--filters",
        f"Name=vpc-id,Values={vpc_id}",
        "--query",
        "Subnets",
    ], default=[]) or []
    private = []
    for subnet in subnets:
        subnet_id = subnet.get("SubnetId") if isinstance(subnet, dict) else None
        if not subnet_id:
            continue
        table = explicit.get(subnet_id, main_table)
        if table and not _route_table_has_igw(table):
            private.append(subnet_id)

    if not private:
        for subnet in subnets:
            subnet_id = subnet.get("SubnetId") if isinstance(subnet, dict) else None
            if not subnet_id:
                continue
            is_private_tag = False
            for tag in subnet.get("Tags", []):
                if tag.get("Key", "").lower() == "tier" and str(tag.get("Value", "")).lower() == "private":
                    is_private_tag = True
                    break
            if is_private_tag and subnet_id not in private:
                private.append(subnet_id)

    if not private:
        for subnet in subnets:
            subnet_id = subnet.get("SubnetId") if isinstance(subnet, dict) else None
            if subnet_id:
                private.append(subnet_id)

    if not private:
        _print_job_event(f"{vpc_id}: could not discover private subnets for interface endpoints")
    return private


def _existing_endpoint_services(vpc_id, region):
    services = _aws_json([
        "ec2",
        "describe-vpc-endpoints",
        *_region_arg(region),
        "--filters",
        f"Name=vpc-id,Values={vpc_id}",
        "--query",
        "VpcEndpoints[?State!='deleted'].ServiceName",
    ], default=[]) or []
    return set(services)


def _ensure_endpoint_sg(vpc_id, vpc_name, vpc_cidr, region):
    sg_name = f"{vpc_name}-vpce-sg"
    existing = _aws_text([
        "ec2",
        "describe-security-groups",
        *_region_arg(region),
        "--filters",
        f"Name=vpc-id,Values={vpc_id}",
        f"Name=tag:Name,Values={sg_name}",
        "--query",
        "SecurityGroups[0].GroupId",
    ])
    if existing:
        return existing
    sg_id = _aws_text([
        "ec2",
        "create-security-group",
        *_region_arg(region),
        "--group-name",
        sg_name,
        "--description",
        "VPC Endpoints",
        "--vpc-id",
        vpc_id,
        "--tag-specifications",
        f"ResourceType=security-group,Tags=[{{Key=Name,Value={sg_name}}}]",
        "--query",
        "GroupId",
    ])
    if not sg_id:
        return ""
    _aws_ok(["ec2", "authorize-security-group-ingress", *_region_arg(region), "--group-id", sg_id, "--protocol", "-1", "--cidr", vpc_cidr])
    return sg_id


def _setup_vpc(vpc_id, region, account_id, max_workers):
    vpc_name = _vpc_name(vpc_id, region)
    vpc_cidr = _vpc_cidr(vpc_id, region)
    if not vpc_cidr:
        _print_job_event(f"{vpc_id}: could not read CIDR; skipping")
        return

    _print_job_event(f"{vpc_id}: repairing networking, endpoints, and flow logs")
    _fix_vpc_networking(vpc_id, vpc_name, vpc_cidr, region, max_workers)

    if not _ensure_vpc_dns_attributes(vpc_id, region):
        _print_job_event(f"{vpc_id}: DNS attributes could not be fully enabled; interface endpoints may fail")

    route_table_ids = _all_route_table_ids(vpc_id, region)
    existing = _existing_endpoint_services(vpc_id, region)

    def create_gateway_endpoint(short):
        service = f"com.amazonaws.{region}.{short}"
        if service in existing:
            _print_job_event(f"{vpc_id}: gateway endpoint {short} already exists")
            return {"endpoint": short, "type": "gateway", "ok": True, "skipped": True}
        if not route_table_ids:
            _print_job_event(f"{vpc_id}: no route tables; skipping gateway endpoint {short}")
            return {"endpoint": short, "type": "gateway", "ok": False, "skipped": True}
        result = _aws_call([
            "ec2",
            "create-vpc-endpoint",
            *_region_arg(region),
            "--vpc-id",
            vpc_id,
            "--service-name",
            service,
            "--vpc-endpoint-type",
            "Gateway",
            "--route-table-ids",
            *route_table_ids,
            "--tag-specifications",
            f"ResourceType=vpc-endpoint,Tags=[{{Key=Name,Value={vpc_name}-vpce-{short}}}]",
        ])
        _print_job_event(f"{vpc_id}: {'created' if result['ok'] else 'failed'} gateway endpoint {short}")
        return {"endpoint": short, "type": "gateway", **result}

    _parallel_map(GATEWAY_ENDPOINTS, create_gateway_endpoint, max_workers)

    private_subnets = _private_subnet_ids(vpc_id, region)
    sg_id = _ensure_endpoint_sg(vpc_id, vpc_name, vpc_cidr, region) if private_subnets else ""

    def create_interface_endpoint(short):
        service = f"com.amazonaws.{region}.{short}"
        if service in existing:
            _print_job_event(f"{vpc_id}: interface endpoint {short} already exists")
            return {"endpoint": short, "type": "interface", "ok": True, "skipped": True}
        if not private_subnets or not sg_id:
            _print_job_event(f"{vpc_id}: skipping interface endpoint {short}; private subnets or security group unavailable")
            return {"endpoint": short, "type": "interface", "ok": False, "skipped": True}
        result = _aws_call([
            "ec2",
            "create-vpc-endpoint",
            *_region_arg(region),
            "--vpc-id",
            vpc_id,
            "--service-name",
            service,
            "--vpc-endpoint-type",
            "Interface",
            "--subnet-ids",
            *private_subnets,
            "--security-group-ids",
            sg_id,
            "--tag-specifications",
            f"ResourceType=vpc-endpoint,Tags=[{{Key=Name,Value={vpc_name}-vpce-{short}}}]",
        ])
        _print_job_event(f"{vpc_id}: {'created' if result['ok'] else 'failed'} interface endpoint {short}")
        return {"endpoint": short, "type": "interface", **result}

    _parallel_map(INTERFACE_ENDPOINTS, create_interface_endpoint, max_workers)

    existing_flowlog = _aws_text([
        "ec2",
        "describe-flow-logs",
        *_region_arg(region),
        "--filter",
        f"Name=resource-id,Values={vpc_id}",
        "Name=log-destination-type,Values=s3",
        "--query",
        "FlowLogs[0].FlowLogId",
    ])
    if existing_flowlog:
        _print_job_event(f"{vpc_id}: S3 flow logs already enabled")
        return
    log_bucket = ensure_s3_log_bucket(region=region, account_id=account_id)
    bucket = log_bucket["bucket"]
    if not log_bucket["ok"]:
        _print_job_event(f"{vpc_id}: could not ensure flow-log bucket {bucket}")
        return
    result = _aws_call([
        "ec2",
        "create-flow-logs",
        *_region_arg(region),
        "--resource-ids",
        vpc_id,
        "--resource-type",
        "VPC",
        "--traffic-type",
        "ALL",
        "--log-destination-type",
        "s3",
        "--log-destination",
        f"arn:aws:s3:::{bucket}",
    ])
    _print_job_event(f"{vpc_id}: {'enabled' if result['ok'] else 'failed to enable'} S3 flow logs")


def _run_vpc_fix_job(args):
    paths = _vpc_job_paths(args.job_id)
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
            account_id = _aws_text(["sts", "get-caller-identity", "--query", "Account"])
            if not account_id:
                raise RuntimeError("could not determine AWS account ID")
            vpc_ids = [item.strip() for item in (args.vpc_ids.split(",") if args.vpc_ids else _all_vpc_ids(region)) if item.strip()]
            _parallel_map(vpc_ids, lambda vpc_id: _setup_vpc(vpc_id, region, account_id, args.max_workers), args.max_workers)
            state["status"] = "SUCCEEDED"
            state["completed_at"] = _utc_now()
            state["region"] = region
            state["vpc_ids"] = ",".join(vpc_ids)
            _write_json(paths["state"], state)
            return 0
        except Exception as exc:
            print(str(exc), file=sys.stderr, flush=True)
            state["status"] = "FAILED"
            state["completed_at"] = _utc_now()
            state["error"] = str(exc)
            _write_json(paths["state"], state)
            return 1
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr


create_vpc_fix_job = _create_vpc_fix_job
describe_vpc_fix_job = _describe_vpc_fix_job
run_vpc_fix_job = _run_vpc_fix_job
