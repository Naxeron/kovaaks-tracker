"""Streamed datasets preserve JSON semantics without materializing whole bodies."""

import functools
import gzip
import io
import json
import weakref
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from kovaaks import dataset_stream, fetch_worker
from kovaaks.api import RequestCancelled
from kovaaks.dataset_stream import PackedCounts, read_dataset_response


class StreamingResponse:
    status_code = 200

    def __init__(self, content, chunk_size=7, on_chunk=None):
        self.body = content
        self.chunk_size = chunk_size
        self.on_chunk = on_chunk
        self.headers = {"ETag": '"streamed"'}
        self.closed = False

    @property
    def content(self):
        raise AssertionError("Streaming must not access response.content")

    def iter_content(self, chunk_size):
        assert 0 < chunk_size <= 64 * 1024
        for start in range(0, len(self.body), self.chunk_size):
            if self.on_chunk:
                self.on_chunk()
            yield self.body[start:start + self.chunk_size]

    def close(self):
        self.closed = True


class InterruptedResponse(StreamingResponse):
    def __init__(self, content, error):
        super().__init__(content)
        self.error = error

    def iter_content(self, chunk_size):
        yield self.body[:len(self.body) // 2]
        raise self.error("stream interrupted")


@pytest.mark.parametrize("values", [[], [None], [1, None, -2, 0, 1 << 62],
                                    [1.5, True, "legacy", 1 << 80]])
def test_packed_counts_preserve_values_nulls_and_fallback_types(values):
    packed = PackedCounts(values)
    assert len(packed) == len(values)
    assert list(packed) == values
    assert packed == values
    assert values == packed
    assert packed[:] == values
    assert [type(value) for value in packed] == [type(value) for value in values]
    if values:
        assert packed[-1] == values[-1]
    with pytest.raises(IndexError):
        packed[len(values)]


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 17])
def test_incremental_decoder_waits_for_complete_numbers_and_values(chunk_size):
    values = [123456789, -120345, 1.25, 1.3e-25, None, True, "é☃", {"quoted": 'x"y'}]
    raw = json.dumps(values, ensure_ascii=False)
    reader = dataset_stream._JsonStream(io.StringIO(raw), lambda: None, chunk_size=chunk_size)
    assert list(reader.array_items()) == values
    reader.finish()


@pytest.mark.parametrize("history_first", [False, True])
@pytest.mark.parametrize("chunk_size", [1, 7, 64])
def test_history_streams_with_either_key_order_and_unicode(monkeypatch, history_first, chunk_size):
    data = {"timestamps": ["2020-01-01T00:00:00", "2020-01-01T00:00:00", "2020-01-01T01:00:00"],
            "history": {"é☃": [123456789, None, -987654321], "other": [1.5, "legacy", True]}}
    if history_first:
        data = dict(reversed(list(data.items())))
    response = StreamingResponse(gzip.compress(json.dumps(data, ensure_ascii=False).encode()), chunk_size=3)
    monkeypatch.setattr(dataset_stream, "_JsonStream",
                        functools.partial(dataset_stream._JsonStream, chunk_size=chunk_size))

    result = read_dataset_response(response, "scenarios_history.json.gz")

    assert result == data
    assert all(isinstance(values, PackedCounts) for values in result["history"].values())


def test_scenarios_are_parsed_incrementally_without_accessing_response_content(monkeypatch):
    data = [{"leaderboardId": "a", "counts": {"entries": 12345}, "scenarioName": "é☃"},
            {"leaderboardId": "b", "counts": {"entries": 54321}, "scenarioName": 'quotes " ] }'}]
    response = StreamingResponse(gzip.compress(json.dumps(data, ensure_ascii=False).encode()), chunk_size=1)
    monkeypatch.setattr(dataset_stream, "_JsonStream",
                        functools.partial(dataset_stream._JsonStream, chunk_size=2))
    assert read_dataset_response(response, "scenarios.json.gz") == data


def test_separately_decoded_scenarios_share_field_keys_including_nested_objects():
    data = [{"leaderboardId": str(index), "counts": {"entries": 12345},
             "scenario_metadata": {"custom_nested_field": index}} for index in range(2)]
    response = StreamingResponse(gzip.compress(json.dumps(data).encode()))
    rows = read_dataset_response(response, "scenarios.json.gz")

    assert rows == data
    assert list(rows[0])[0] is list(rows[1])[0]
    assert next(iter(rows[0]["counts"])) is next(iter(rows[1]["counts"]))
    assert next(iter(rows[0]["scenario_metadata"])) is next(iter(rows[1]["scenario_metadata"]))


def test_field_key_pool_is_bounded_and_preserves_unknown_and_duplicate_fields():
    data = [{f"unusual_field_{index}": index} for index in range(1000)]
    reader = dataset_stream._JsonStream(io.StringIO(json.dumps(data)), lambda: None, chunk_size=97)

    assert list(reader.array_items()) == data
    reader.finish()
    assert len(reader._field_keys) == dataset_stream._MAX_FIELD_KEYS

    reader = dataset_stream._JsonStream(io.StringIO('[{"repeated":1,"repeated":2}]'), lambda: None,
                                        chunk_size=3)
    assert list(reader.array_items()) == [{"repeated": 2}]
    reader.finish()


def test_key_pool_does_not_keep_finished_stream_reader_alive():
    reader = dataset_stream._JsonStream(io.StringIO('[{"shared_field":1}]'), lambda: None)
    reader_ref = weakref.ref(reader)
    values = list(reader.array_items())
    reader.finish()
    del reader

    assert reader_ref() is None
    assert values == [{"shared_field": 1}]


@pytest.mark.parametrize("text", [
    '[{"leaderboardId":"a","counts":{}}]{}',
    '[{"leaderboardId":"a","counts":{}},]',
    '[{"leaderboardId":"a","counts":{"entries":01}}]',
    '[{"leaderboardId":"a","counts":{"entries":1e}}]',
    '[{"leaderboardId":"a","counts":{}}',
    '[{"leaderboardId":"a","counts":{}}] trailing',
    '[{"leaderboardId":"a","counts":null}]',
])
def test_malformed_or_trailing_json_is_rejected(monkeypatch, text):
    monkeypatch.setattr(dataset_stream, "_JsonStream",
                        functools.partial(dataset_stream._JsonStream, chunk_size=1))
    with pytest.raises(ValueError):
        read_dataset_response(StreamingResponse(gzip.compress(text.encode())), "scenarios.json.gz")


@pytest.mark.parametrize("text", [
    '{"history":{"a":[1,2]},"timestamps":["2020-01-01"]}',
    '{"history":{"a":1},"timestamps":["2020-01-01"]}',
    '{"history":{"a":[1]},"timestamps":[null]}',
    '{"history":{"a":[1]}}',
    '{"timestamps":[]}',
    '{"history":{},"timestamps":[],}',
])
def test_invalid_history_schema_is_rejected(text):
    with pytest.raises(ValueError):
        read_dataset_response(StreamingResponse(gzip.compress(text.encode())), "scenarios_history.json.gz")


@pytest.mark.parametrize("damage", ["truncated", "crc", "trailing_member"])
def test_complete_json_requires_valid_gzip_footer_and_no_trailing_member(damage):
    content = gzip.compress(b"[]")
    if damage == "truncated":
        content = content[:-1]
    elif damage == "crc":
        content = content[:-8] + bytes([content[-8] ^ 1]) + content[-7:]
    else:
        content += gzip.compress(b"unexpected")
    with pytest.raises((ValueError, OSError, EOFError)):
        read_dataset_response(StreamingResponse(content), "scenarios.json.gz")


def test_cancellation_closes_stream_without_publishing_download_metadata(monkeypatch):
    app = SimpleNamespace(_cfg={}, _fetch_cancelled=False)

    def cancel():
        app._fetch_cancelled = True

    response = StreamingResponse(gzip.compress(b"[]"), on_chunk=cancel)
    request = Mock(return_value=response)
    save = Mock()
    monkeypatch.setattr(fetch_worker, "api_request_with_retry", request)
    monkeypatch.setattr(fetch_worker, "save_config", save)

    with pytest.raises(RequestCancelled):
        fetch_worker.fetch_gzip_json_from_github("scenarios.json.gz", app)

    assert response.closed
    assert app._cfg == {}
    assert not hasattr(app, "_dataset_download_cache")
    save.assert_not_called()
    assert request.call_args.kwargs["stream"] is True


def test_failed_stream_never_exposes_partial_history_or_new_validator(monkeypatch):
    content = b'{"history":{"valid":[100],"bad":[1,2]},"timestamps":["2020-01-01"]}'
    response = StreamingResponse(gzip.compress(content))
    app = SimpleNamespace(_cfg={"last_etags": {"scenarios_history.json.gz": "previous"}},
                          _scores_cache={"entry_history": {"existing": {"2020-01-01": 10}}})
    monkeypatch.setattr(fetch_worker, "api_request_with_retry", Mock(return_value=response))
    save = Mock()
    monkeypatch.setattr(fetch_worker, "save_config", save)

    assert fetch_worker.fetch_gzip_json_from_github("scenarios_history.json.gz", app) is None
    assert app._scores_cache == {"entry_history": {"existing": {"2020-01-01": 10}}}
    assert app._cfg["last_etags"]["scenarios_history.json.gz"] == "previous"
    assert response.closed
    save.assert_not_called()


def test_terminal_http_error_response_is_closed(monkeypatch):
    response = StreamingResponse(b"")
    response.status_code = 403
    error = requests.HTTPError(response=response)
    monkeypatch.setattr(fetch_worker, "api_request_with_retry", Mock(side_effect=error))

    assert fetch_worker.fetch_gzip_json_from_github("scenarios.json.gz", SimpleNamespace(_cfg={})) is None
    assert response.closed


def test_response_cleanup_error_does_not_mask_success(monkeypatch):
    response = StreamingResponse(gzip.compress(b"[]"))
    response.close = Mock(side_effect=OSError("cleanup failed"))
    monkeypatch.setattr(fetch_worker, "api_request_with_retry", Mock(return_value=response))
    monkeypatch.setattr(fetch_worker, "save_config", Mock())

    assert fetch_worker.fetch_gzip_json_from_github("scenarios.json.gz", SimpleNamespace(_cfg={})) == []
    response.close.assert_called_once()


@pytest.mark.parametrize("error", [requests.ConnectionError, requests.Timeout])
def test_interrupted_body_retries_after_closing_without_publishing_partial_data(monkeypatch, error):
    filename = "scenarios_history.json.gz"
    data = {"timestamps": ["2020-01-01"], "history": {"downloaded": [100]}}
    content = gzip.compress(json.dumps(data).encode())
    failed = InterruptedResponse(content, error)
    successful = StreamingResponse(content)
    successful.headers["ETag"] = "complete"
    app = SimpleNamespace(_cfg={"last_etags": {filename: "previous"}},
                          _scores_cache={"entry_history": {"existing": {"2020-01-01": 10}}})
    request = Mock()

    def get_response(*args, **kwargs):
        if request.call_count == 1:
            return failed
        assert failed.closed
        assert app._cfg["last_etags"][filename] == "previous"
        assert not hasattr(app, "_dataset_download_cache")
        return successful

    request.side_effect = get_response
    save = Mock()
    sleep = Mock()
    monkeypatch.setattr(fetch_worker, "api_request_with_retry", request)
    monkeypatch.setattr(fetch_worker, "save_config", save)
    monkeypatch.setattr(fetch_worker.time, "sleep", sleep)

    assert fetch_worker.fetch_gzip_json_from_github(filename, app) == data
    assert request.call_count == 2
    assert failed.closed and successful.closed
    assert app._scores_cache == {"entry_history": {"existing": {"2020-01-01": 10}}}
    assert app._cfg["last_etags"][filename] == "complete"
    save.assert_called_once_with(app._cfg)
    assert sum(call.args[0] for call in sleep.call_args_list) == pytest.approx(0.5)
    assert max(call.args[0] for call in sleep.call_args_list) <= 0.1


@pytest.mark.parametrize("error", [requests.ConnectionError, requests.Timeout])
def test_interrupted_body_retry_budget_preserves_previous_metadata(monkeypatch, error):
    filename = "scenarios.json.gz"
    responses = [InterruptedResponse(gzip.compress(b"[]"), error) for _ in range(3)]
    app = SimpleNamespace(_cfg={"last_etags": {filename: "previous"}})
    request = Mock(side_effect=responses)
    save = Mock()
    sleep = Mock()
    monkeypatch.setattr(fetch_worker, "api_request_with_retry", request)
    monkeypatch.setattr(fetch_worker, "save_config", save)
    monkeypatch.setattr(fetch_worker.time, "sleep", sleep)

    assert fetch_worker.fetch_gzip_json_from_github(filename, app) is None
    assert request.call_count == 3
    assert all(response.closed for response in responses)
    assert app._cfg["last_etags"][filename] == "previous"
    assert not hasattr(app, "_dataset_download_cache")
    save.assert_not_called()
    assert sum(call.args[0] for call in sleep.call_args_list) == pytest.approx(1.5)


def test_cancellation_during_stream_retry_backoff_stops_before_next_request(monkeypatch):
    response = InterruptedResponse(gzip.compress(b"[]"), requests.ConnectionError)
    app = SimpleNamespace(_cfg={}, _fetch_cancelled=False)
    request = Mock(return_value=response)
    save = Mock()

    def cancel_while_waiting(seconds):
        assert response.closed
        app._fetch_cancelled = True

    monkeypatch.setattr(fetch_worker, "api_request_with_retry", request)
    monkeypatch.setattr(fetch_worker, "save_config", save)
    monkeypatch.setattr(fetch_worker.time, "sleep", cancel_while_waiting)

    with pytest.raises(RequestCancelled):
        fetch_worker.fetch_gzip_json_from_github("scenarios.json.gz", app)

    request.assert_called_once()
    save.assert_not_called()
    assert app._cfg == {}
    assert not hasattr(app, "_dataset_download_cache")
