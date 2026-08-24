

def test_a_role_keeps_the_same_identity_across_runs(tmp_path):
    """Identity.create mints a fresh keypair every call, so every run used to
    introduce six strangers: yesterday's architect and today's had no visible
    relation, and a channel cannot be read as a conversation between people
    who are new each time."""
    from dume.collaboration.buzz import role_identity

    store = tmp_path / "identities.json"
    first = role_identity("architect", store)
    second = role_identity("architect", store)
    assert first.pubkey == second.pubkey
    assert role_identity("implementer", store).pubkey != first.pubkey
    assert store.stat().st_mode & 0o777 == 0o600


def test_every_role_is_seated_in_a_channel_it_speaks_in():
    """A role in a channel it never posts to is noise in the member list; a
    role posting into a channel it is not in reads as an outsider."""
    from dume.collaboration.buzz import ROLE_CHANNELS, SPACE_CHANNELS
    from dume.control.orchestrator import Orchestrator

    for role, names in ROLE_CHANNELS.items():
        assert names, f"{role} is seated nowhere"
        for name in names:
            assert name in SPACE_CHANNELS, f"{role} sits in unknown {name}"

    for step, role in Orchestrator.STEP_SPEAKER.items():
        assert role in ROLE_CHANNELS, f"{step} speaks as unknown role {role}"
        channel = Orchestrator.STEP_CHANNEL.get(step)
        assert channel in ROLE_CHANNELS[role], (
            f"{role} speaks {step} into {channel}, which it is not seated in")
