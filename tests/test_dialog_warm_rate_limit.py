"""A cold entity lookup re-warms the cache with get_dialogs(), at most once per interval."""

import asyncio

import pytest

from telegram_mcp import runtime


class _ColdClient:
    """Raises ValueError for every id until get_dialogs() has been called."""

    def __init__(self, fail_dialogs=None):
        self.dialog_calls = 0
        self.fail_dialogs = fail_dialogs
        self.warm = False

    async def get_dialogs(self):
        self.dialog_calls += 1
        if self.fail_dialogs is not None:
            raise self.fail_dialogs
        self.warm = True

    async def get_entity(self, identifier):
        if not self.warm:
            raise ValueError("cold entity cache")
        return f"entity:{identifier}"


class _MissingClient(_ColdClient):
    """A chat that is simply not there: warming never helps."""

    async def get_entity(self, identifier):
        raise ValueError("no such chat")


@pytest.fixture(autouse=True)
def _clock_and_env(monkeypatch):
    monkeypatch.delenv(runtime._DIALOG_WARM_INTERVAL_ENV, raising=False)
    now = [1000.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: now[0])

    async def _connected(client):
        return None

    monkeypatch.setattr(runtime, "ensure_connected", _connected)
    return now


async def _miss(client, identifier=-1001):
    with pytest.raises(ValueError):
        await runtime._resolve_with_retries("get_entity", identifier, client, "chat")


@pytest.mark.asyncio
async def test_repeated_misses_inside_the_interval_fetch_dialogs_once(_clock_and_env):
    client = _MissingClient()

    for _ in range(5):
        await _miss(client)
        _clock_and_env[0] += 10

    assert client.dialog_calls == 1


@pytest.mark.asyncio
async def test_a_miss_after_the_interval_warms_again(monkeypatch, _clock_and_env):
    monkeypatch.setenv(runtime._DIALOG_WARM_INTERVAL_ENV, "2")
    client = _MissingClient()

    await _miss(client)
    _clock_and_env[0] += 119
    await _miss(client)
    assert client.dialog_calls == 1

    _clock_and_env[0] += 2
    await _miss(client)
    assert client.dialog_calls == 2


@pytest.mark.asyncio
async def test_the_default_interval_is_five_minutes(_clock_and_env):
    client = _MissingClient()

    await _miss(client)
    _clock_and_env[0] += 299
    await _miss(client)
    assert client.dialog_calls == 1

    _clock_and_env[0] += 2
    await _miss(client)
    assert client.dialog_calls == 2


@pytest.mark.asyncio
async def test_the_limit_is_per_client(_clock_and_env):
    first, second = _MissingClient(), _MissingClient()

    await _miss(first)
    await _miss(second)

    assert (first.dialog_calls, second.dialog_calls) == (1, 1)


@pytest.mark.asyncio
async def test_zero_interval_turns_the_limit_off(monkeypatch):
    monkeypatch.setenv(runtime._DIALOG_WARM_INTERVAL_ENV, "0")
    client = _MissingClient()

    for _ in range(3):
        await _miss(client)

    assert client.dialog_calls == 3


@pytest.mark.parametrize("raw", ["soon", "-3"])
@pytest.mark.asyncio
async def test_a_bad_interval_does_not_disable_the_limit(monkeypatch, raw):
    monkeypatch.setenv(runtime._DIALOG_WARM_INTERVAL_ENV, raw)
    client = _MissingClient()

    await _miss(client)
    await _miss(client)

    # Garbage falls back to the default; a negative value clamps to "no limit".
    assert client.dialog_calls == (1 if raw == "soon" else 2)


@pytest.mark.asyncio
async def test_a_failed_warm_still_counts(_clock_and_env):
    """FLOOD_WAIT on get_dialogs must not be retried on every following miss."""
    client = _MissingClient(fail_dialogs=RuntimeError("FLOOD_WAIT"))

    await _miss(client)
    await _miss(client)

    assert client.dialog_calls == 1


@pytest.mark.asyncio
async def test_the_startup_warm_counts_toward_the_limit(_clock_and_env):
    client = _MissingClient()
    runtime.note_dialogs_warmed(client)  # what the runner does before its own fetch

    await _miss(client)

    assert client.dialog_calls == 0


@pytest.mark.asyncio
async def test_a_cold_lookup_still_resolves_after_one_warm(_clock_and_env):
    client = _ColdClient()

    result = await runtime._resolve_with_retries("get_entity", 42, client, "chat")

    assert result == "entity:42"
    assert client.dialog_calls == 1


@pytest.mark.asyncio
async def test_concurrent_misses_share_one_fetch(_clock_and_env):
    client = _MissingClient()

    await asyncio.gather(*(_miss(client) for _ in range(6)))

    assert client.dialog_calls == 1
