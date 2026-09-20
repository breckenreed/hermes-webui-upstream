"""
Gateway chat: tool-call details are hydrated from the agent's state.db.

Background
----------
When chat runs through the Hermes Gateway, tool RESULTS never cross the wire.
The agent's ``tool_progress_callback`` contract passes ``preview=None,
args=None`` on ``tool.completed`` (``agent/tool_executor.py``), so the runs API
can report that a tool finished but not what it returned. Both gateway session
payloads therefore used to be built with a hardcoded ``tool_calls=[]``, and the
browser rendered tool cards with an empty Output tab — a failed tool showed no
reason at all.

The result is not lost, only off-stream: the agent persists the whole
transcript to its own ``state.db``, which multi-container deployments already
share with the WebUI, and a WebUI session id IS the agent-side session id. So
the payload re-reads it locally and feeds the rows to
``_extract_tool_calls_from_messages`` — the same builder the in-process path
uses.

Coverage
--------
1.  A stored assistant+tool pair becomes a tool-call summary carrying the
    tool's real output
2.  ... including a failing tool's error text, which is what the empty Output
    tab hid
3.  Hydration reuses the in-process builder rather than a parallel pairing
    implementation
4.  Unknown session / blank id / missing db / unreadable db all fail soft to []
5.  A malformed tool_calls blob does not discard the rest of the transcript
6.  Both gateway payload sites hydrate instead of passing []
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from api.agent_sessions import read_agent_transcript_rows  # noqa: E402
from api.streaming import _extract_tool_calls_from_messages  # noqa: E402

SESSION = "329ef9c20d43"


def _make_state_db(tmp_path: Path, rows: list[dict]) -> Path:
    """Build a minimal state.db shaped like the agent's messages table."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE messages ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " session_id TEXT, role TEXT, content TEXT,"
        " tool_call_id TEXT, tool_calls TEXT, tool_name TEXT)"
    )
    for row in rows:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, tool_name)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                row.get("session_id", SESSION),
                row.get("role"),
                row.get("content"),
                row.get("tool_call_id"),
                row.get("tool_calls"),
                row.get("tool_name"),
            ),
        )
    conn.commit()
    conn.close()
    return db_path


def _call(tool_id: str, name: str, arguments: dict) -> str:
    return json.dumps(
        [{
            "id": tool_id,
            "call_id": tool_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]
    )


@pytest.fixture
def terminal_db(tmp_path):
    return _make_state_db(
        tmp_path,
        [
            {"role": "user", "content": "list the vault"},
            {"role": "assistant", "content": "", "tool_calls": _call("tid-ok", "terminal", {"command": "ls /vault"})},
            {
                "role": "tool",
                "tool_call_id": "tid-ok",
                "tool_name": "terminal",
                "content": json.dumps({"status": "success", "output": "daily-kanban.md\nnotes.md"}),
            },
        ],
    )


class TestHydration:

    def test_tool_output_reaches_the_summary(self, terminal_db):
        rows = read_agent_transcript_rows(terminal_db, SESSION)
        calls = _extract_tool_calls_from_messages(rows)

        assert len(calls) == 1
        call = calls[0]
        assert call["name"] == "terminal"
        assert call["tid"] == "tid-ok"
        assert call["args"] == {"command": "ls /vault"}
        # The point of the whole change: the tool's OUTPUT, not just its name.
        assert "daily-kanban.md" in str(call["snippet"])

    def test_failing_tool_carries_its_reason(self, tmp_path):
        """A red 'Failed' card with an empty Output tab was the visible bug."""
        db = _make_state_db(
            tmp_path,
            [
                {"role": "assistant", "content": "", "tool_calls": _call("tid-bad", "terminal", {"command": "cd /nope"})},
                {
                    "role": "tool",
                    "tool_call_id": "tid-bad",
                    "tool_name": "terminal",
                    "content": json.dumps({"output": "cd: /nope: No such file or directory", "exit_code": 126}),
                },
            ],
        )
        calls = _extract_tool_calls_from_messages(read_agent_transcript_rows(db, SESSION))

        assert len(calls) == 1
        assert "No such file or directory" in str(calls[0]["snippet"])

    def test_rows_are_shaped_for_the_in_process_builder(self, terminal_db):
        """Hydration must feed the existing builder, not re-pair calls itself.

        If these keys ever stop matching what ``_extract_tool_calls_from_messages``
        reads, hydration silently yields [] — so pin the contract here.
        """
        rows = read_agent_transcript_rows(terminal_db, SESSION)

        assistant = next(r for r in rows if r["role"] == "assistant")
        tool = next(r for r in rows if r["role"] == "tool")
        assert isinstance(assistant["tool_calls"], list)
        assert assistant["tool_calls"][0]["function"]["name"] == "terminal"
        assert tool["tool_call_id"] == "tid-ok"

    def test_malformed_tool_calls_blob_keeps_the_rest(self, tmp_path):
        db = _make_state_db(
            tmp_path,
            [
                {"role": "assistant", "content": "", "tool_calls": "{not json"},
                {"role": "assistant", "content": "", "tool_calls": _call("tid-ok", "terminal", {"command": "ls"})},
                {
                    "role": "tool",
                    "tool_call_id": "tid-ok",
                    "content": json.dumps({"output": "ok"}),
                },
            ],
        )
        rows = read_agent_transcript_rows(db, SESSION)

        assert len(rows) == 3
        assert "tool_calls" not in rows[0]          # dropped, not fatal
        assert isinstance(rows[1]["tool_calls"], list)
        assert _extract_tool_calls_from_messages(rows)


class TestFailsSoft:
    """Every failure must degrade to [] — the value the call sites used before."""

    def test_unknown_session(self, terminal_db):
        assert read_agent_transcript_rows(terminal_db, "deadbeefcafe") == []

    def test_blank_session_id(self, terminal_db):
        assert read_agent_transcript_rows(terminal_db, "") == []
        assert read_agent_transcript_rows(terminal_db, None) == []

    def test_missing_database(self, tmp_path):
        assert read_agent_transcript_rows(tmp_path / "absent.db", SESSION) == []

    def test_database_without_messages_table(self, tmp_path):
        db = tmp_path / "state.db"
        sqlite3.connect(db).close()
        assert read_agent_transcript_rows(db, SESSION) == []

    def test_helper_swallows_a_broken_db_path(self):
        from api.gateway_chat import _gateway_tool_calls_from_agent_db

        assert _gateway_tool_calls_from_agent_db("") == []


class TestCallSites:

    def test_both_gateway_payloads_hydrate(self):
        source = (REPO_ROOT / "api" / "gateway_chat.py").read_text(encoding="utf-8")

        assert "tool_calls=[]" not in source, (
            "a gateway session payload still hardcodes an empty tool_calls list"
        )
        assert source.count("_gateway_tool_calls_from_agent_db(") >= 3  # def + 2 sites


class TestUsageHydration:
    """Cumulative token usage for the context pill, from the same state.db.

    A gateway run reports usage on the wire but wrote none of it back, so a
    gateway-only conversation showed "0 tokens used" while the agent's
    ``sessions`` row held the real totals.
    """

    @staticmethod
    def _usage_db(tmp_path: Path, **columns) -> Path:
        db_path = tmp_path / "state.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE sessions ("
            " id TEXT PRIMARY KEY, input_tokens INTEGER, output_tokens INTEGER,"
            " cache_read_tokens INTEGER, cache_write_tokens INTEGER,"
            " estimated_cost_usd REAL)"
        )
        conn.execute(
            "INSERT INTO sessions (id, input_tokens, output_tokens, cache_read_tokens,"
            " cache_write_tokens, estimated_cost_usd) VALUES (?, ?, ?, ?, ?, ?)",
            (
                SESSION,
                columns.get("input_tokens"),
                columns.get("output_tokens"),
                columns.get("cache_read_tokens"),
                columns.get("cache_write_tokens"),
                columns.get("estimated_cost_usd"),
            ),
        )
        conn.commit()
        conn.close()
        return db_path

    def test_totals_are_mapped_to_session_fields(self, tmp_path):
        from api.agent_sessions import read_agent_session_usage

        db = self._usage_db(
            tmp_path,
            input_tokens=401260,
            output_tokens=14680,
            cache_read_tokens=12,
            estimated_cost_usd=1.5,
        )
        usage = read_agent_session_usage(db, SESSION)

        assert usage["input_tokens"] == 401260
        assert usage["output_tokens"] == 14680
        assert usage["cache_read_tokens"] == 12
        assert usage["estimated_cost"] == 1.5
        assert isinstance(usage["input_tokens"], int)

    def test_absent_and_zero_columns_are_omitted_not_zeroed(self, tmp_path):
        """A caller does dict.update, so a missing total must not clobber one it has."""
        from api.agent_sessions import read_agent_session_usage

        db = self._usage_db(tmp_path, input_tokens=100, output_tokens=0)
        usage = read_agent_session_usage(db, SESSION)

        assert usage == {"input_tokens": 100}
        assert "output_tokens" not in usage
        assert "cache_read_tokens" not in usage

    def test_no_fabricated_context_percentage(self, tmp_path):
        """last_prompt_tokens / threshold_tokens have no trustworthy source here.

        messages.token_count is NULL in the agent's schema and the auto-compress
        threshold is computed by the compressor with caps this side cannot see,
        so a percentage would be invented. Fail closed: omit them.
        """
        from api.agent_sessions import read_agent_session_usage

        usage = read_agent_session_usage(
            self._usage_db(tmp_path, input_tokens=5, output_tokens=6), SESSION
        )

        assert "last_prompt_tokens" not in usage
        assert "threshold_tokens" not in usage
        assert "context_length" not in usage

    @pytest.mark.parametrize("session_id", ["", None, "deadbeefcafe"])
    def test_fails_soft(self, tmp_path, session_id):
        from api.agent_sessions import read_agent_session_usage

        db = self._usage_db(tmp_path, input_tokens=1)
        assert read_agent_session_usage(db, session_id) == {}

    def test_missing_db_and_missing_table(self, tmp_path):
        from api.agent_sessions import read_agent_session_usage

        assert read_agent_session_usage(tmp_path / "absent.db", SESSION) == {}
        empty = tmp_path / "empty.db"
        sqlite3.connect(empty).close()
        assert read_agent_session_usage(empty, SESSION) == {}

    def test_success_writeback_hydrates_usage(self):
        source = (REPO_ROOT / "api" / "gateway_chat.py").read_text(encoding="utf-8")
        assert "read_agent_session_usage" in source
        # Must land before the session is persisted, or the totals are lost.
        assert source.index("read_agent_session_usage") < source.rindex("s.save()")
