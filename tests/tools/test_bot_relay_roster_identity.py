"""Tests: roster publisher shards + backend-install identity merge (``tools/bot_relay.py``).

The Desktop pushes each gateway its view of the agents on the OTHER connections.
More than one window (or Desktop) pushes, and the same machine is reachable over
two routes — Mac Studio as ``100-90-126-123-9119`` and as ``mac-studio-via-sh``,
SH as ``sh`` and as ``150-158-136-129-39911``. With one roster file replaced
whole, those views erased each other and the roster size flapped ``n=5↔8`` /
``n=10↔24`` (2 982 ``roster changed`` broadcasts in 11 h), so every client redrew
periodically. Contracts pinned here:

- a named publisher owns ONE shard and can only replace that shard;
- rows of one ``(install_id, profile)`` are ONE row, the spare connection ids
  kept as ``aliases`` so ``message_agent`` still routes to them;
- a shard nobody refreshed is ignored past its TTL;
- a caller that names no publisher (older Desktop) replaces the single
  ``roster.json`` exactly as before the contract.
"""

import json
import textwrap
import time

import pytest

from tools import bot_relay
from tools.bot_mode_dm import message_agent_tool

STUDIO_INSTALL = "0096c20b30dc42f3"
STUDIO_LAN = "100-90-126-123-9119"
STUDIO_VIA_SH = "mac-studio-via-sh"


@pytest.fixture()
def root(tmp_path):
    return tmp_path


def _row(connection_id, *, profile="default", handle="hermes", install_id=None, title="", online=None):
    row = {
        "profile": profile,
        "handle": handle,
        "connection_id": connection_id,
        "connection_label": connection_id.upper(),
        "title": title,
        "description": "",
    }
    if install_id:
        row["install_id"] = install_id
    if online is not None:
        row["online"] = online
    return row


def _shards(root):
    return sorted((bot_relay.relay_root(root) / bot_relay.PUBLISHERS_DIR).glob("*.json"))


# ── one machine, two routes, two publishers: ONE row ────────────────────────


def test_two_publishers_alternating_on_one_machine_read_back_one_group(root):
    """The 11 h flap: window 1 sees Studio over its LAN route, window 2 over the SH tunnel.
    Both push the SAME install. Alternating pushes must serve one row, identically every time."""
    lan = [_row(STUDIO_LAN, install_id=STUDIO_INSTALL, title="Studio")]
    via_sh = [_row(STUDIO_VIA_SH, install_id=STUDIO_INSTALL, title="Studio")]
    p1, p2 = f"{STUDIO_INSTALL}:win-1", f"{STUDIO_INSTALL}:win-2"

    # Both windows have published at least once (the steady state after a restart).
    bot_relay.write_remote_roster(root, lan, publisher=p1)
    bot_relay.write_remote_roster(root, via_sh, publisher=p2)

    reads = set()
    for _ in range(3):
        bot_relay.write_remote_roster(root, lan, publisher=p1)
        reads.add(json.dumps(bot_relay.read_remote_roster(root), sort_keys=True))
        bot_relay.write_remote_roster(root, via_sh, publisher=p2)
        reads.add(json.dumps(bot_relay.read_remote_roster(root), sort_keys=True))

    roster = bot_relay.read_remote_roster(root)
    assert len(roster) == 1, roster  # one machine is one bot, however many routes it has
    assert len(reads) == 1, reads  # ... and the merged view does not flap between pushes
    assert roster[0]["install_id"] == STUDIO_INSTALL
    assert roster[0]["connection_id"] == STUDIO_LAN  # canonical: reachable from both, sorted first
    assert roster[0]["aliases"] == [STUDIO_VIA_SH]
    assert len(_shards(root)) == 2  # one shard per publishing window


def test_pre_contract_replace_path_still_flaps(root):
    """The bug this contract names: with no publisher every push replaces the one roster file,
    so two views of the same machine alternate the surviving connection id."""
    reads = []
    for connection_id in (STUDIO_LAN, STUDIO_VIA_SH, STUDIO_LAN):
        bot_relay.write_remote_roster(root, [_row(connection_id, install_id=STUDIO_INSTALL)])
        reads.append([row["connection_id"] for row in bot_relay.read_remote_roster(root)])

    assert reads == [[STUDIO_LAN], [STUDIO_VIA_SH], [STUDIO_LAN]]


def test_canonical_route_does_not_flip_with_push_order(root):
    """Two routes, two windows: the canonical id is the one most windows can route to, and it is
    decided by that count, not by who pushed last — a flipping id would read as a new row."""
    a = f"{STUDIO_INSTALL}:win-1"
    b = f"{STUDIO_INSTALL}:win-2"

    bot_relay.write_remote_roster(root, [_row(STUDIO_LAN, install_id=STUDIO_INSTALL)], publisher=a)
    first = bot_relay.read_remote_roster(root)[0]["connection_id"]

    bot_relay.write_remote_roster(root, [_row(STUDIO_VIA_SH, install_id=STUDIO_INSTALL)], publisher=b)
    second = bot_relay.read_remote_roster(root)[0]["connection_id"]

    # Re-publish in the opposite order: the merged row must not change.
    bot_relay.write_remote_roster(root, [_row(STUDIO_VIA_SH, install_id=STUDIO_INSTALL)], publisher=b)
    bot_relay.write_remote_roster(root, [_row(STUDIO_LAN, install_id=STUDIO_INSTALL)], publisher=a)

    assert first == second == bot_relay.read_remote_roster(root)[0]["connection_id"]


def test_rows_without_a_shared_install_id_stay_apart(root):
    """An older backend reports no install_id: two routes cannot be proven to be one machine, so
    they stay two rows — exactly the pre-contract behaviour, never a wrong collapse."""
    bot_relay.write_remote_roster(root, [_row(STUDIO_LAN)], publisher=f"{STUDIO_INSTALL}:win-1")
    bot_relay.write_remote_roster(root, [_row(STUDIO_VIA_SH)], publisher=f"{STUDIO_INSTALL}:win-2")

    assert sorted(row["connection_id"] for row in bot_relay.read_remote_roster(root)) == sorted(
        [STUDIO_LAN, STUDIO_VIA_SH]
    )


# ── TTL ─────────────────────────────────────────────────────────────────────


def test_publisher_shard_past_its_ttl_is_ignored(root):
    """A window that stopped publishing must stop pinning its rows — but only at READ time: the
    last push a wobbling window managed is still there if it comes back within the TTL."""
    bot_relay.write_remote_roster(root, [_row(STUDIO_VIA_SH, install_id=STUDIO_INSTALL)],
                                  publisher=f"{STUDIO_INSTALL}:win-1")
    now = time.time()

    assert len(bot_relay.read_remote_roster(root, now=now)) == 1
    assert bot_relay.read_remote_roster(root, now=now + bot_relay.DEFAULT_PUBLISHER_TTL_SECONDS + 1) == []
    # Ignored, not deleted: one missed tick costs nothing.
    assert len(_shards(root)) == 1


def test_expired_shard_is_pruned_by_the_next_write(root):
    """Shards are files: a closed window's shard must not pile up forever."""
    stale = f"{STUDIO_INSTALL}:win-1"
    bot_relay.write_remote_roster(root, [_row(STUDIO_LAN, install_id=STUDIO_INSTALL)], publisher=stale)
    [path] = _shards(root)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["updated_at"] = int(time.time()) - bot_relay.DEFAULT_PUBLISHER_TTL_SECONDS - 60
    path.write_text(json.dumps(payload), encoding="utf-8")

    bot_relay.write_remote_roster(root, [_row(STUDIO_VIA_SH, install_id=STUDIO_INSTALL)],
                                  publisher=f"{STUDIO_INSTALL}:win-2")

    assert len(_shards(root)) == 1
    assert [row["connection_id"] for row in bot_relay.read_remote_roster(root)] == [STUDIO_VIA_SH]


def test_legacy_file_survives_a_shard_ttl_expiry(root):
    """The legacy file keeps its own (caller-side) freshness rule; a dead shard must not take it
    down with it."""
    bot_relay.write_remote_roster(root, [_row("cloud-1", title="Moxie")])
    bot_relay.write_remote_roster(root, [_row(STUDIO_LAN, install_id=STUDIO_INSTALL)],
                                  publisher=f"{STUDIO_INSTALL}:win-1")

    later = time.time() + bot_relay.DEFAULT_PUBLISHER_TTL_SECONDS + 1

    assert [row["connection_id"] for row in bot_relay.read_remote_roster(root, now=later)] == ["cloud-1"]


# ── compatibility: no publisher == today's behaviour ────────────────────────


def test_callers_without_publisher_keep_the_single_file_replace(root):
    """An older Desktop names no publisher: it must keep writing the one roster.json, whole."""
    rows = [
        _row("cloud-1", title="Moxie"),
        _row("ssh-vps", profile="researcher", handle="researcher"),
    ]

    assert bot_relay.write_remote_roster(root, rows) == 2

    path = bot_relay.relay_root(root) / bot_relay.ROSTER_FILE
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert set(payload) == {"updated_at", "agents"}  # the pre-contract shape, no publisher key
    assert _shards(root) == []  # a legacy caller mints no shard
    # What was written is what reads back — no aliases, no extra keys.
    assert bot_relay.read_remote_roster(root) == payload["agents"]

    # A second legacy push replaces the file whole: its own rows, never a union.
    assert bot_relay.write_remote_roster(root, [_row("cloud-1", title="Moxie")]) == 1
    assert bot_relay.read_remote_roster(root) == [
        {
            "profile": "default",
            "handle": "hermes",
            "connection_id": "cloud-1",
            "connection_label": "CLOUD-1",
            "title": "Moxie",
            "description": "",
        }
    ]


def test_shards_and_the_legacy_file_are_served_as_one_roster(root):
    """Compatibility window: an old Desktop still replaces roster.json while a new one pushes its
    shard. Neither view may erase the other."""
    bot_relay.write_remote_roster(root, [_row("cloud-1", title="Moxie")])
    bot_relay.write_remote_roster(root, [_row(STUDIO_VIA_SH, install_id=STUDIO_INSTALL)],
                                  publisher=f"{STUDIO_INSTALL}:win-1")

    assert sorted(row["connection_id"] for row in bot_relay.read_remote_roster(root)) == [
        "cloud-1",
        STUDIO_VIA_SH,
    ]


# ── install_id validation ───────────────────────────────────────────────────


def test_malformed_install_id_is_dropped_without_losing_the_row(root):
    """Rows are untrusted RPC input. A bad annotation must never cost a live teammate its
    routing entry — the row survives, uncollapsed, exactly like an older backend's."""
    bad = {**_row("cloud-1"), "install_id": "bad id!'"}
    bot_relay.write_remote_roster(root, [bad])

    [row] = bot_relay.read_remote_roster(root)

    assert "install_id" not in row
    assert row["connection_id"] == "cloud-1"


def test_install_id_is_kept_when_well_formed(root):
    bot_relay.write_remote_roster(root, [_row("cloud-1", install_id=STUDIO_INSTALL)])

    assert bot_relay.read_remote_roster(root)[0]["install_id"] == STUDIO_INSTALL


# ── aliases keep routing (message_agent) ────────────────────────────────────


def _managed_home(tmp_path):
    home = tmp_path / ".hermes"
    (home / "profiles" / "researcher").mkdir(parents=True, exist_ok=True)
    (home / "profiles" / "researcher" / "profile.yaml").write_text(
        textwrap.dedent(
            """\
            description: teammate for tests
            ui_meta:
              hermes-bots:
                shape: cloud
            """
        ),
        encoding="utf-8",
    )
    return home


class _FakeDB:
    def __init__(self, home, title):
        self.db_path = str(home / "state.db")
        self._title = title

    def get_session_title(self, _sid):
        return self._title


class _FakeAgent:
    def __init__(self, home, title="Bot Chat"):
        self._session_db = _FakeDB(home, title)
        self.session_id = "sess-1"
        self._session_title_hint = None
        self._bot_mode_protocol = True
        self.tools: list = []
        self.valid_tool_names: set = set()


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    from tools import bot_mode_probe

    bot_mode_probe._reset_cache_for_tests()
    yield
    bot_mode_probe._reset_cache_for_tests()


def test_merged_row_keeps_alias_routes_and_message_agent_still_resolves(tmp_path, monkeypatch):
    """The merged row drops no route: every connection id of the group addresses the same bot, and
    a DM aimed at the alias lands as an envelope for the canonical route the Desktop can deliver."""
    home = _managed_home(tmp_path)
    bot_relay.write_remote_roster(home, [_row(STUDIO_LAN, install_id=STUDIO_INSTALL)], publisher="w1:x")
    bot_relay.write_remote_roster(home, [_row(STUDIO_VIA_SH, install_id=STUDIO_INSTALL)], publisher="w2:x")

    roster = bot_relay.read_remote_roster(home)
    [row] = roster

    assert row["aliases"] == [STUDIO_VIA_SH]
    assert bot_relay.resolve_remote_target(f"hermes@{STUDIO_LAN}", roster)["connection_id"] == STUDIO_LAN
    assert bot_relay.resolve_remote_target(f"hermes@{STUDIO_VIA_SH}", roster)["connection_id"] == STUDIO_LAN
    # A target named by the alias route is not read as "vanished from a fresh roster".
    assert bot_relay._target_liveness(home, {"connection_id": STUDIO_VIA_SH, "profile": "default"}) is None

    monkeypatch.setattr("tools.bot_mode_dm._spawn_delivery", lambda *a, **k: json.dumps({"status": "queued"}))
    out = json.loads(message_agent_tool(target=f"hermes@{STUDIO_VIA_SH}", message="ping", agent=_FakeAgent(home)))

    assert out.get("status") == "queued", out
    [envelope] = bot_relay.claim_pending_envelopes(home)
    assert envelope["target_connection"] == STUDIO_LAN
    assert envelope["target_profile"] == "default"


def test_sender_stamp_qualifies_an_alias_route(tmp_path):
    """A relayed DM stamped with the sender's alias route must re-stamp to the form this gateway
    can reply to — the merged row's canonical/qualified form, not a dangling alias."""
    home = _managed_home(tmp_path)
    bot_relay.write_remote_roster(home, [{**_row(STUDIO_LAN, install_id=STUDIO_INSTALL, title="CoS Bot"),
                                          "handle": "hermes"}], publisher="w1:x")
    bot_relay.write_remote_roster(home, [{**_row(STUDIO_VIA_SH, install_id=STUDIO_INSTALL, title="CoS Bot"),
                                          "handle": "hermes"}], publisher="w2:x")

    roster = bot_relay.read_remote_roster(home)
    stamp = "Message from 🤖 CoS Bot (@hermes): are we done?"
    out = bot_relay.qualify_sender_stamp(stamp, "hermes", STUDIO_VIA_SH, roster, {"hermes"})

    assert out == "Message from 🤖 CoS Bot (@cos-bot): are we done?"
