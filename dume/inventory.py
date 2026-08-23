"""WP-001 — host hardware, OS and capacity inventory.

Every local-model, Buzz and DUM-E deployment decision reads from this file. No
inference backend may be selected from assumptions, so everything here is
probed from the running host and recorded with the command that produced it.

The failure modes this module exists to prevent are the ones the work package
names: choosing a context length because the *model* supports it rather than
because the *host* can sustain it, and confusing total VRAM with the VRAM that
is actually free after display and runtime overhead.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

# Bytes per parameter, by weight format. Used for the capacity envelope only —
# an envelope is an upper bound that rules options out, never a promise.
BYTES_PER_PARAM = {"bf16": 2.0, "fp16": 2.0, "fp8": 1.0, "int8": 1.0,
                   "q6_k": 0.82, "q5_k_m": 0.71, "q4_k_m": 0.60, "q3_k_m": 0.47}

# Runtime overhead that is not weights: CUDA context, activations, fragmentation.
# Deliberately generous; an envelope that flatters the host is worthless.
RUNTIME_OVERHEAD_FRACTION = 0.12


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out after {timeout}s"


def probe_os() -> dict:
    release = {}
    osr = Path("/etc/os-release")
    if osr.is_file():
        for line in osr.read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                release[k] = v.strip('"')
    return {
        "system": platform.system(),
        "kernel": platform.release(),
        "distribution": release.get("PRETTY_NAME"),
        "version_id": release.get("VERSION_ID"),
        "architecture": platform.machine(),
        "python": platform.python_version(),
    }


def probe_cpu_memory() -> dict:
    info: dict = {"logical_cpus": os.cpu_count()}
    rc, out = _run(["lscpu"])
    if rc == 0:
        for key, field in (("Model name", "model"), ("Socket(s)", "sockets"),
                           ("Core(s) per socket", "cores_per_socket"),
                           ("Thread(s) per core", "threads_per_core")):
            m = re.search(rf"^{re.escape(key)}:\s+(.+)$", out, re.M)
            if m:
                info[field] = m.group(1).strip()
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        text = meminfo.read_text()
        for key, field in (("MemTotal", "ram_total_bytes"),
                           ("MemAvailable", "ram_available_bytes"),
                           ("SwapTotal", "swap_total_bytes")):
            m = re.search(rf"^{key}:\s+(\d+) kB$", text, re.M)
            if m:
                info[field] = int(m.group(1)) * 1024
    return info


def probe_gpus() -> dict:
    """Enumerate GPUs, recording *free* VRAM separately from total.

    Total VRAM is what a datasheet says. Free VRAM is what a server can actually
    claim, and on a host whose second GPU also drives a display those two
    numbers are not the same.
    """
    fields = ("index,name,memory.total,memory.free,memory.used,driver_version,"
              "compute_mode,pci.bus_id")
    rc, out = _run(["nvidia-smi", f"--query-gpu={fields}",
                    "--format=csv,noheader,nounits"])
    result: dict = {"probe_command": "nvidia-smi --query-gpu=...", "available": rc == 0}
    if rc != 0:
        result["error"] = out.strip()
        result["gpus"] = []
        return result
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 8:
            continue
        gpus.append({
            "index": int(parts[0]), "name": parts[1],
            "vram_total_bytes": int(float(parts[2])) * 1024 ** 2,
            "vram_free_bytes": int(float(parts[3])) * 1024 ** 2,
            "vram_used_bytes": int(float(parts[4])) * 1024 ** 2,
            "driver_version": parts[5], "compute_mode": parts[6],
            "pci_bus_id": parts[7],
        })
    result["gpus"] = gpus
    result["vram_total_bytes"] = sum(g["vram_total_bytes"] for g in gpus)
    result["vram_free_bytes"] = sum(g["vram_free_bytes"] for g in gpus)
    rc, topo = _run(["nvidia-smi", "topo", "-m"])
    result["topology"] = topo.strip() if rc == 0 else None
    # NVLink changes what tensor parallelism costs. PCIe/NODE links do not make
    # two cards one card.
    result["nvlink"] = bool(re.search(r"\bNV\d+\b", topo)) if rc == 0 else None
    rc, cuda = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    m = re.search(r"CUDA Version:\s*([\d.]+)", _run(["nvidia-smi"])[1])
    result["cuda_version"] = m.group(1) if m else None
    return result


def probe_storage() -> dict:
    """Every mounted filesystem with real capacity, plus which can host weights."""
    rc, out = _run(["findmnt", "-rno", "TARGET,SOURCE,FSTYPE,OPTIONS"])
    seen: dict[str, dict] = {}
    if rc == 0:
        for line in out.strip().splitlines():
            parts = line.split(None, 3)
            if len(parts) < 3:
                continue
            target, source, fstype = parts[0], parts[1], parts[2]
            options = parts[3] if len(parts) > 3 else ""
            if fstype in {"tmpfs", "devtmpfs", "squashfs", "proc", "sysfs",
                          "cgroup2", "devpts", "efivarfs", "autofs", "overlay",
                          "fuse.portal", "tracefs", "securityfs", "pstore",
                          "bpf", "configfs", "debugfs", "hugetlbfs", "mqueue",
                          "fusectl", "ramfs", "binfmt_misc", "nsfs"}:
                continue
            if source.startswith("/dev/loop"):
                continue
            try:
                usage = shutil.disk_usage(target)
            except OSError:
                continue
            seen[target] = {
                "mountpoint": target, "source": source, "fstype": fstype,
                "total_bytes": usage.total, "free_bytes": usage.free,
                "used_bytes": usage.used,
                "percent_used": round(usage.used / usage.total * 100, 1) if usage.total else None,
                "read_only": "ro" in options.split(","),
            }
    filesystems = sorted(seen.values(), key=lambda f: -f["free_bytes"])
    return {
        "filesystems": filesystems,
        "largest_free": filesystems[0] if filesystems else None,
    }


def probe_container_and_network() -> dict:
    info: dict = {}
    for tool in ("docker", "podman"):
        rc, out = _run([tool, "--version"], timeout=15)
        info[tool] = out.strip().splitlines()[0] if rc == 0 else None
    rc, out = _run(["ss", "-tlnH"])
    ports = set()
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                m = re.search(r":(\d+)$", parts[3])
                if m:
                    ports.add(int(m.group(1)))
    info["listening_ports"] = sorted(ports)
    # Serving-stack defaults that are already taken must be known before a
    # bring-up runbook tells someone to use them.
    info["common_serving_ports_in_use"] = sorted(
        p for p in (8000, 8080, 30000, 11434, 5000, 1234) if p in ports)
    return info


def capacity_envelope(gpus: dict, storage: dict, cpu: dict) -> dict:
    """What this host can and cannot hold. An envelope rules things out."""
    vram_total = gpus.get("vram_total_bytes", 0)
    vram_free = gpus.get("vram_free_bytes", 0)
    usable = int(vram_free * (1 - RUNTIME_OVERHEAD_FRACTION))
    envelope = {
        "vram_total_bytes": vram_total,
        "vram_free_bytes": vram_free,
        "vram_usable_for_weights_bytes": usable,
        "runtime_overhead_fraction": RUNTIME_OVERHEAD_FRACTION,
        "nvlink": gpus.get("nvlink"),
        "gpu_count": len(gpus.get("gpus", [])),
    }
    # A 27B model at each candidate precision, against usable VRAM.
    params = 27e9
    fits = {}
    for fmt, bpp in BYTES_PER_PARAM.items():
        need = int(params * bpp)
        fits[fmt] = {
            "weight_bytes": need,
            "fits_in_usable_vram": need <= usable,
            "headroom_bytes": usable - need,
        }
    envelope["model_27b_weight_fit"] = fits
    largest = storage.get("largest_free") or {}
    envelope["model_cache_candidate"] = {
        "mountpoint": largest.get("mountpoint"),
        "free_bytes": largest.get("free_bytes"),
    }
    root_free = next((f["free_bytes"] for f in storage["filesystems"]
                      if f["mountpoint"] == "/"), 0)
    envelope["root_filesystem_free_bytes"] = root_free
    envelope["root_can_hold_bf16_27b"] = root_free > int(params * 2.0)
    envelope["ram_total_bytes"] = cpu.get("ram_total_bytes")
    return envelope


def classify(envelope: dict) -> tuple[str, list[str]]:
    """Assign the host class the work package requires, and say why."""
    reasons: list[str] = []
    gpu_count = envelope["gpu_count"]
    usable = envelope["vram_usable_for_weights_bytes"]
    gib = 1024 ** 3

    if gpu_count == 0:
        reasons.append("no CUDA device was enumerated")
        return "CPU_HEAVY" if (envelope.get("ram_total_bytes") or 0) > 128 * gib \
            else "REMOTE_GPU_REQUIRED", reasons

    reasons.append(f"{gpu_count} GPU(s), {usable / gib:.1f} GiB usable for weights "
                   f"after a {envelope['runtime_overhead_fraction']:.0%} runtime reserve")
    if not envelope["model_27b_weight_fit"]["bf16"]["fits_in_usable_vram"]:
        reasons.append("a 27B model in bf16 does not fit in usable VRAM; "
                       "quantisation or a smaller model is required, not a "
                       "larger context")
    if envelope["nvlink"] is False and gpu_count > 1:
        reasons.append("GPUs are linked over PCIe/NODE, not NVLink; tensor "
                       "parallelism pays an interconnect cost")
    if not envelope["root_can_hold_bf16_27b"]:
        reasons.append(
            f"the root filesystem holds {envelope['root_filesystem_free_bytes'] / gib:.0f} GiB "
            "free, which cannot host bf16 27B weights; the model cache must live "
            "on another filesystem")

    if usable >= 80 * gib:
        return "HIGH_THROUGHPUT_GPU", reasons
    return "SINGLE_GPU_CONSTRAINED", reasons


def collect() -> dict:
    os_info = probe_os()
    cpu = probe_cpu_memory()
    gpus = probe_gpus()
    storage = probe_storage()
    net = probe_container_and_network()
    envelope = capacity_envelope(gpus, storage, cpu)
    host_class, reasons = classify(envelope)
    return {
        "schema": "dume.host_inventory/1",
        "os": os_info,
        "cpu_memory": cpu,
        "gpu": gpus,
        "storage": storage,
        "container_and_network": net,
        "capacity_envelope": envelope,
        "host_class": host_class,
        "classification_reasons": reasons,
    }
