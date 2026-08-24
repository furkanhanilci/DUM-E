"""Serving the local models, per DUME-ADR-0004 and DUME-ADR-0008.

The profile is llama.cpp CUDA in a container, Q4_K_M, on one GPU. Two reasons
that ADR records and this module encodes: the image's `NVIDIA_REQUIRE_CUDA`
admits exactly this host's driver, and llama.cpp is the only candidate stack
that grammar-constrains Qwen's XML tool call instead of parsing it afterwards.

The two mandatory gotchas from that research are checked here rather than left
in prose, because a warning that nothing enforces is a warning that gets
skipped:

* the chat template's `raise_exception` asserts make every request 400 before a
  token is emitted, so the template is inspected and patched;
* `-ctk` and `-ctv` must be the same quantisation type or flash attention
  silently disables and prefill collapses, so they are set together and never
  taken from two separate arguments.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

IMAGE = "ghcr.io/ggml-org/llama.cpp:server-cuda"
MODEL_DIR = Path("/media/otonom/DATADRIVE1/dume-model-cache")
INTERNAL_PORT = 8080  # inside the container — the image's own healthcheck polls
                      # http://localhost:8080/health, so serving anywhere else
                      # leaves a working server permanently marked unhealthy


@dataclass(frozen=True)
class Profile:
    """One local model on one GPU.

    Two of these run at once, and they exist as separate *families* rather than
    two copies of one model on purpose: a reviewer from the implementer's family
    shares its blind spot, so a second instance of the same weights would buy
    availability and no independence at all.
    """
    runtime_id: str
    container: str
    model_file: str
    port: int
    gpu_index: int
    family: str
    context: int = 65536
    # Two slots, not four. Each slot gets context/parallel, so four slots left
    # 16384 per conversation — and a six-thousand-token prompt plus a tool call
    # carrying a whole file does not fit in that. We commission one package at a
    # time; the other two slots bought nothing and cost the budget that matters.
    parallel: int = 2
    kv_type: str = "q8_0"

    def model_path(self) -> Path:
        return MODEL_DIR / self.model_file

    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


# Chosen from the measured envelope, not from either model's native ceiling.
# 24 GiB per card; ~16 GiB and ~14 GiB of weights; KV is 64 KiB/token bf16 for
# Qwen and only its 16 full-attention layers grow with context. The 48
# Gated-DeltaNet layers cost per *sequence*, which is why the cap that matters
# is concurrency.
QWEN = Profile("qwen-local", "dume-qwen", "Qwen3.8-27B-UD-Q4_K_M.gguf",
               8000, 0, "qwen")
MISTRAL = Profile("mistral-local", "dume-mistral",
                  "Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf",
                  8001, 1, "mistral")

PROFILES = {p.runtime_id: p for p in (QWEN, MISTRAL)}

# Kept so existing callers and the CLI default keep working.
MODEL_FILE = QWEN.model_file
CONTAINER = QWEN.container
PORT = QWEN.port

# Module-level aliases for the default profile, kept so existing callers read
# unchanged.
CONTEXT = QWEN.context
PARALLEL = QWEN.parallel
KV_TYPE = QWEN.kv_type
GPU_INDEX = QWEN.gpu_index


@dataclass
class ServeResult:
    started: bool
    detail: str
    endpoint: str = ""
    container: str = CONTAINER

    def as_dict(self) -> dict:
        return asdict(self)


def model_path(profile: "Profile" = None) -> Path:
    return (profile or QWEN).model_path()


def preflight(profile: "Profile" = None) -> dict:
    """Everything that must be true before a serve attempt is worth making."""
    profile = profile or QWEN
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    docker = shutil.which("docker")
    check("docker", docker is not None, docker or "docker is not on PATH")

    image = subprocess.run(
        ["docker", "images", "-q", IMAGE], capture_output=True, text=True)
    check("image", bool(image.stdout.strip()),
          f"{IMAGE} " + ("present" if image.stdout.strip() else "not pulled"))

    path = profile.model_path()
    size = path.stat().st_size if path.is_file() else 0
    check("artefact", path.is_file() and size > 8 * 1024 ** 3,
          f"{path} — {size / 1024**3:.1f} GiB" if size else f"{path} absent")

    # Whether a container can reach the GPU is settled by trying, not by
    # reading the runtimes list. A modern toolkit passes devices through CDI
    # without registering a runtime named "nvidia", so inferring from that list
    # reports a working host as blocked — which is what it did here.
    check(*_gpu_passthrough(profile.gpu_index))

    driver = subprocess.run(
        ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
        capture_output=True, text=True)
    version = driver.stdout.strip().splitlines()[0] if driver.returncode == 0 else ""
    # The image config admits driver>=535,<536. A newer driver is fine for CUDA
    # but this image will refuse it, and that is worth knowing before the serve
    # rather than from a container that exits immediately.
    major = version.split(".")[0] if version else ""
    check("driver_matches_image", major == "535",
          f"driver {version or 'unknown'}; this image admits >=535,<536")

    return {"schema": "dume.qwen_preflight/1", "checks": checks,
            "ready": all(c["passed"] for c in checks),
            "blocking": [c["check"] for c in checks if not c["passed"]]}


def _gpu_passthrough(gpu_index: int = 0) -> tuple[str, bool, str]:
    """Can a container actually see the GPU? Probed with an image already local."""
    images = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True, text=True).stdout.splitlines()
    probe_image = next((i for i in images if "<none>" not in i), None)
    if probe_image is None:
        return ("gpu_passthrough", False,
                "no local image to probe with; cannot establish this either way")
    result = subprocess.run(
        ["docker", "run", "--rm", "--gpus", f"device={gpu_index}",
         "--entrypoint", "/bin/sh", probe_image, "-c", f"ls /dev/nvidia{gpu_index}"],
        capture_output=True, text=True, timeout=120)
    ok = result.returncode == 0 and f"/dev/nvidia{gpu_index}" in result.stdout
    return ("gpu_passthrough", ok,
            f"/dev/nvidia{gpu_index} visible inside a container" if ok
            else (result.stderr.strip() or result.stdout.strip())[:160])


def template_asserts(path: Path | None = None, profile: "Profile" = None) -> dict:
    """Does the packaged chat template refuse before emitting a token?

    Reported rather than silently worked around: if the asserts are present, a
    patched template must be supplied, and knowing that before the first request
    is the difference between a five-second fix and an afternoon.
    """
    path = path or (profile or QWEN).model_path()
    if not path.is_file():
        return {"checked": False, "detail": f"{path} absent"}
    # The template lives in GGUF key-value metadata, which sits at the front of
    # the file, so a bounded header read finds it without paging sixteen
    # gigabytes. The bound is stated in the result: a clean report here means
    # "not in the first 64 MiB", not "not present anywhere", and the difference
    # matters because the authoritative answer comes from the server refusing
    # or not refusing a real request.
    from .gguf import GGUFError, chat_template
    try:
        template = chat_template(path)
    except GGUFError as exc:
        return {"checked": False, "detail": f"metadata unreadable: {exc}"}
    if not template:
        return {"checked": False, "detail": "no chat_template in the metadata"}

    guards = re.findall(r"""raise_exception\(\s*['"]([^'"]*)""", template)

    # Not every guard is a defect. Most of these refuse a genuinely malformed
    # request — an empty message list, an unknown role, a tool call with no
    # function name — and a template that accepted those would be worse. What
    # matters is which guards refuse a *legitimate* pattern, because those are
    # the ones that turn a working harness into a 400 with no token emitted.
    HAZARDS = (
        (r"system message must be at the beginning",
         "refuses a system message injected mid-conversation, which is exactly "
         "what a harness does when it re-states a role or a constraint between "
         "turns (llama.cpp issue #27367)"),
    )
    MULTIMODAL = r"image|video|audio|vision"

    hazards, multimodal, validation = [], [], []
    for guard in guards:
        matched = next((why for pattern, why in HAZARDS
                        if re.search(pattern, guard, re.I)), None)
        if matched:
            hazards.append({"guard": guard.strip(), "why": matched})
        elif re.search(MULTIMODAL, guard, re.I):
            multimodal.append(guard.strip())
        else:
            validation.append(guard.strip())

    return {"checked": True,
            "raise_exception_count": len(guards),
            "hazards": hazards,
            "multimodal_only": len(multimodal),
            "legitimate_validation": len(validation),
            "needs_patch": bool(hazards),
            "detail": (
                f"{len(hazards)} guard(s) refuse a legitimate pattern: "
                + "; ".join(h["guard"] for h in hazards)
                + " — supply --chat-template-file, or never move a system "
                  "message off the front"
                if hazards else
                f"{len(guards)} guard(s), all of them either multimodal-only "
                f"({len(multimodal)}) or refusals of a genuinely malformed "
                f"request ({len(validation)})")}


def serve_command(profile: "Profile" = None) -> list[str]:
    """The exact command, so the report and the run cannot disagree."""
    profile = profile or QWEN
    return [
        "docker", "run", "-d", "--name", profile.container,
        "--gpus", f"device={profile.gpu_index}",
        "-p", f"{profile.port}:{INTERNAL_PORT}",
        "-v", f"{MODEL_DIR}:/models:ro",
        IMAGE,
        "-m", f"/models/{profile.model_file}",
        "--host", "0.0.0.0", "--port", str(INTERNAL_PORT),
        "-ngl", "999",              # every layer on the GPU
        "-sm", "none",              # one card; -sm row is dead on CUDA and
                                    # -sm tensor crashes on this architecture
        "-fa", "on",
        "-c", str(profile.context),
        "-np", str(profile.parallel),   # concurrency is the constraint
        "-ctk", profile.kv_type, "-ctv", profile.kv_type,  # must match or
                                    # flash attention silently disables
        "--jinja",                  # required for tool-call grammar
    ]


def serve(force: bool = False, profile: "Profile" = None) -> ServeResult:
    profile = profile or QWEN
    ready = preflight(profile)
    if not ready["ready"]:
        return ServeResult(False, "preflight failed: " + ", ".join(ready["blocking"]))

    existing = subprocess.run(
        ["docker", "ps", "-aq", "-f", f"name=^{profile.container}$"],
        capture_output=True, text=True).stdout.strip()
    if existing:
        if not force:
            return ServeResult(False, f"container {profile.container} already "
                                      "exists; pass force to replace it",
                               container=profile.container)
        subprocess.run(["docker", "rm", "-f", profile.container], capture_output=True)

    result = subprocess.run(serve_command(profile), capture_output=True, text=True)
    if result.returncode != 0:
        return ServeResult(False, result.stderr.strip()[:400],
                           container=profile.container)
    return ServeResult(True, f"container started: {result.stdout.strip()[:12]}",
                       endpoint=profile.endpoint(), container=profile.container)


def health(timeout: float = 5.0, profile: "Profile" = None) -> dict:
    """Is it answering, and with what?"""
    url = (profile or QWEN).endpoint() + "/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode())
        return {"up": True, "endpoint": url,
                "models": [m.get("id") for m in data.get("data", [])]}
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return {"up": False, "endpoint": url, "detail": f"{type(exc).__name__}: {exc}"}


def logs(lines: int = 40, profile: "Profile" = None) -> str:
    result = subprocess.run(["docker", "logs", "--tail", str(lines),
                             (profile or QWEN).container],
                            capture_output=True, text=True)
    return (result.stdout + result.stderr).strip()


def tool_call_probe(timeout: float = 180.0, profile: "Profile" = None) -> dict:
    profile = profile or QWEN
    """The check that actually matters: does it call a tool correctly?

    Throughput is not what the harness needs from a local model — reliable tool
    calling and structured output are. Measuring that is WP-009's job; this is
    the smallest honest version of it, so the serving decision is not declared
    good on the basis that the process started.
    """
    payload = {
        "model": "local",
        "messages": [{"role": "user",
                      "content": "What is the capacity envelope of GPU 0? "
                                 "Use the tool."}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "gpu_capacity",
                "description": "Report usable VRAM for a GPU index.",
                "parameters": {
                    "type": "object",
                    "properties": {"index": {"type": "integer"}},
                    "required": ["index"]},
            }}],
        "tool_choice": "auto",
        "max_tokens": 256,
        "temperature": 0,
    }
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        profile.endpoint() + "/chat/completions", data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return {"called": False, "detail": f"HTTP {exc.code}: "
                                           f"{exc.read().decode(errors='replace')[:300]}"}
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        return {"called": False, "detail": f"{type(exc).__name__}: {exc}"}

    message = (data.get("choices") or [{}])[0].get("message", {})
    calls = message.get("tool_calls") or []
    if not calls:
        return {"called": False, "detail": "no tool_calls in the response",
                "content": (message.get("content") or "")[:200]}
    call = calls[0].get("function", {})
    try:
        arguments = json.loads(call.get("arguments") or "{}")
        parsed = True
    except json.JSONDecodeError:
        arguments, parsed = {}, False
    return {"called": True, "name": call.get("name"),
            "arguments_parsed": parsed, "arguments": arguments,
            "correct_name": call.get("name") == "gpu_capacity",
            "detail": "tool call emitted and parsed"
                      if parsed else "tool call emitted but arguments are not JSON"}
