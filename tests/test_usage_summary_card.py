import json
from datetime import datetime, timezone

from services.cowork_agent.adapters import usage_common
from services.cowork_agent.adapters.claude_code import usage


def test_card_cache_tracks_file_changes_and_date_windows(tmp_path):
    transcript = tmp_path / "session.jsonl"
    timestamp = datetime.now(timezone.utc).isoformat()
    record = {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {"id": "one", "usage": {"input_tokens": 3, "output_tokens": 2}},
    }
    transcript.write_text("invalid JSON\n" + json.dumps(record) + "\n")
    path = str(transcript)
    reads = []

    def card_entries(path, **window):
        reads.append(window)
        return usage._card_entries(path, **window)

    uncached = usage_common.Source(discover=lambda: [path], parse_file=usage.parse_file)
    cached = usage_common.Source(
        discover=lambda: [path], parse_file=usage.parse_file, card_entries=card_entries,
    )
    week = {"days": 7, "tz": "utc"}
    day = {"days": 1, "tz": "utc"}

    base = usage_common.summary_card(uncached, window=week)
    assert base["totalTokens"] == 5
    assert usage_common.summary_card(cached, window=week) == base
    assert usage_common.summary_card(cached, window=week) == base
    assert len(reads) == 1
    assert usage_common.summary_card(cached, window=day) == usage_common.summary_card(uncached, window=day)
    assert len(reads) == 2

    with transcript.open("a") as handle:
        handle.write(json.dumps(record) + "\n")  # Same message id counts once.
        record["message"]["id"] = "two"
        handle.write(json.dumps(record) + "\n")
    updated = usage_common.summary_card(cached, window=week)
    assert updated == usage_common.summary_card(uncached, window=week)
    assert updated["totalTokens"] == 10
    assert len(reads) == 3

    transcript.unlink()
    assert usage_common.summary_card(cached, window=week)["dailyCost"] == []


def test_card_does_not_cache_a_failed_read(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.touch()
    reads = 0

    def card_entries(path, **window):
        nonlocal reads
        reads += 1
        yield {"role": "user"}
        if reads == 1:
            raise OSError("read interrupted")

    source = usage_common.Source(
        discover=lambda: [str(transcript)], parse_file=usage.parse_file, card_entries=card_entries,
    )
    window = {"days": 1, "tz": "utc"}
    assert len(usage_common.summary_card(source, window=window)["dailyCost"]) == 1
    assert len(usage_common.summary_card(source, window=window)["dailyCost"]) == 1
    assert len(usage_common.summary_card(source, window=window)["dailyCost"]) == 1
    assert reads == 2
