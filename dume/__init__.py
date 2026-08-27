"""DUM-E — a supervised multi-model software engineering harness.

DUM-E owns the glue that turns a work package into a merge-eligible candidate
and nothing else: packetisation, cohort and runtime binding, model fallback,
worktree execution, review and verification sequencing, minimal durable state
and a deterministic merge-eligibility gate.

What it deliberately is not: an agent framework, a model server, an editor or a
project. The target it builds is bound as configuration, and the harness holds
no opinion about what that target is.
"""
__version__ = "0.1.0.dev0"
