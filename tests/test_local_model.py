"""The local model server, and what qualification is allowed to conclude.

The live tests skip when nothing is serving on :8000, because a test that needs
a 16 GiB model loaded should say so rather than fail as though the code were
wrong.
"""
import json
import urllib.error
import urllib.request

import pytest

from dume.runtimes import qualification, qwen
from dume.runtimes.gguf import GGUFError, chat_template, metadata
from dume.runtimes.profiles import NoEligibleRuntime, Runtime, RuntimeRegistry


def _serving() -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/v1/models", timeout=2).read(1)
        return True
    except (urllib.error.URLError, OSError):
        return False


needs_server = pytest.mark.skipif(not _serving(), reason="no local model on :8000")
needs_artefact = pytest.mark.skipif(not qwen.model_path().is_file(),
                                    reason="model artefact not staged")


# ---- the serve command encodes the decisions, not just the paths --------

def test_the_serve_command_keeps_the_kv_types_together():
    """Mismatched -ctk/-ctv silently disables flash attention and collapses
    prefill. They are set from one constant so they cannot drift apart."""
    command = qwen.serve_command()
    assert command[command.index("-ctk") + 1] == command[command.index("-ctv") + 1]


def test_the_serve_command_avoids_the_split_modes_that_break_this_architecture():
    command = qwen.serve_command()
    assert command[command.index("-sm") + 1] == "none"


def test_the_operating_context_is_not_the_models_native_ceiling():
    """262144 is what the model supports; it is not what this host sustains."""
    assert qwen.CONTEXT < 262144
    assert "-c" in qwen.serve_command()


def test_concurrency_is_capped_because_it_is_the_real_constraint():
    """The Gated-DeltaNet layers cost per sequence, not per token."""
    command = qwen.serve_command()
    assert int(command[command.index("-np") + 1]) <= 8


def test_preflight_reports_every_blocker_rather_than_the_first():
    result = qwen.preflight()
    assert {c["check"] for c in result["checks"]} >= {
        "docker", "image", "artefact", "gpu_passthrough", "driver_matches_image"}
    assert result["ready"] == (not result["blocking"])


# ---- reading the artefact ----------------------------------------------

@needs_artefact
def test_the_chat_template_is_read_from_metadata_not_grepped():
    """A 4 MiB window once reported this template clean. It has 11 guards."""
    template = chat_template(qwen.model_path())
    assert template and len(template) > 1000
    report = qwen.template_asserts()
    assert report["template_found_in_window"], \
        "the scan did not reach the metadata, so a clean result proves nothing"


@needs_artefact
def test_metadata_identifies_the_architecture():
    data = metadata(qwen.model_path(), keys=("general.architecture",))
    assert data["general.architecture"]


def test_a_non_gguf_file_is_refused(tmp_path):
    path = tmp_path / "not.gguf"
    path.write_bytes(b"NOPE" + b"\x00" * 64)
    with pytest.raises(GGUFError, match="not a GGUF file"):
        metadata(path)


# ---- qualification ------------------------------------------------------

@needs_server
def test_the_model_emits_a_well_formed_tool_call():
    """Throughput is not what the harness needs from a local model. This is."""
    result = qwen.tool_call_probe()
    assert result["called"], result
    assert result["correct_name"] and result["arguments_parsed"]


@needs_server
def test_qualification_names_the_trial_that_refused_a_role():
    result = qualification.qualify("qwen-local", "http://127.0.0.1:8000/v1",
                                   repeats=2)
    assert result.trials
    for role, why in result.refused_roles.items():
        assert why.startswith("failed: "), (role, why)
    assert set(result.qualified_roles) & set(result.refused_roles) == set()


def test_qualification_requires_more_of_a_reviewer_than_of_an_implementer():
    """A reviewer that agrees with everything is worse than no reviewer,
    because it produces evidence."""
    import inspect
    source = inspect.getsource(qualification)
    assert "refuses_an_unsound_claim" in source
    assert "admits_uncertainty" in source


# ---- the finding that matters -------------------------------------------

def test_one_local_model_cannot_commission_anything_alone():
    """It can implement. It cannot review its own work, and the refusal must
    name every runtime and why — a BLOCKED_RUNTIME with no reason is an outage
    report nobody can act on."""
    registry = RuntimeRegistry([Runtime(
        "qwen-local", "local", "qwen", "AVAILABLE", family="qwen", local=True,
        qualified_roles=["implementer", "spec_reviewer", "code_reviewer",
                         "verifier"])])
    implementer = registry.bind("implementer", agent_id="wp/implementer")
    assert implementer.runtime_id == "qwen-local"

    with pytest.raises(NoEligibleRuntime) as exc:
        registry.bind("spec_reviewer", already_bound={"implementer": implementer},
                      family_independent_of=("implementer",),
                      agent_id="wp/spec_reviewer")
    message = str(exc.value)
    assert "same model family" in message
    assert "assurance does not shrink" in message
