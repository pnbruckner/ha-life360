"""Regression test for completed Life360 request task retention."""

from __future__ import annotations

import asyncio
import gc
import weakref

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.life360.config_flow import Life360ConfigFlow
from custom_components.life360.const import DOMAIN
from custom_components.life360.coordinator import RequestError

from .test_init import cfg_options


async def test_completed_client_request_tasks_are_released(
    hass: HomeAssistant,
) -> None:
    """Completed client request tasks should not remain reachable."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=Life360ConfigFlow.VERSION,
        options=cfg_options(1, verbosity=3),
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    await asyncio.sleep(0.1)

    coordinator = entry.runtime_data.coordinator
    task_refs: dict[str, list[weakref.ReferenceType[asyncio.Task]]] = {
        "Make client request to aid1": [],
        "Make request to aid1": [],
        "Monitor failed request to aid1": [],
    }
    create_background_task = entry.async_create_background_task

    def capture_background_task(*args, **kwargs):
        task = create_background_task(*args, **kwargs)
        if task.get_name() in task_refs:
            task_refs[task.get_name()].append(weakref.ref(task))
        return task

    entry.async_create_background_task = capture_background_task

    async def request() -> dict[str, str]:
        return {"payload": "x" * 10_000}

    for _ in range(100):
        assert await coordinator._client_request(  # noqa: SLF001
            "aid1", request, msg="regression test"
        ) == {"payload": "x" * 10_000}

    await asyncio.sleep(0)
    gc.collect()

    assert {name: len(refs) for name, refs in task_refs.items()} == {
        "Make client request to aid1": 100,
        "Make request to aid1": 100,
        "Monitor failed request to aid1": 100,
    }
    assert all(ref() is None for refs in task_refs.values() for ref in refs)


async def test_failed_account_cancels_in_flight_request(
    hass: HomeAssistant,
) -> None:
    """An account failure should stop its in-flight client requests."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=Life360ConfigFlow.VERSION,
        options=cfg_options(1, verbosity=3),
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    started = asyncio.Event()

    async def request() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        coordinator._client_request("aid1", request, msg="failure test")  # noqa: SLF001
    )
    await started.wait()
    coordinator._acct_data["aid1"].failed.set()  # noqa: SLF001

    assert await asyncio.wait_for(task, timeout=1) is RequestError.NO_DATA


async def test_external_cancellation_wins_account_failure(
    hass: HomeAssistant,
) -> None:
    """External cancellation should propagate when an account also fails."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=Life360ConfigFlow.VERSION,
        options=cfg_options(1, verbosity=3),
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    started = asyncio.Event()

    async def request() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        coordinator._request("aid1", request, msg="cancellation race")  # noqa: SLF001
    )
    await started.wait()
    task.cancel()
    coordinator._acct_data["aid1"].failed.set()  # noqa: SLF001

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_account_failure_wins_cancellation_resistant_request(
    hass: HomeAssistant,
) -> None:
    """Account failure should win even if a request suppresses cancellation."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=Life360ConfigFlow.VERSION,
        options=cfg_options(1, verbosity=3),
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    started = asyncio.Event()

    async def request() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
            return "unexpected request completion"
        except asyncio.CancelledError:
            return "request ignored cancellation"

    task = asyncio.create_task(
        coordinator._request("aid1", request, msg="failure race")  # noqa: SLF001
    )
    await started.wait()
    coordinator._acct_data["aid1"].failed.set()  # noqa: SLF001

    assert await asyncio.wait_for(task, timeout=1) is RequestError.NO_DATA


async def test_external_cancellation_wins_resistant_request(
    hass: HomeAssistant,
) -> None:
    """External cancellation should win even if the request suppresses it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=Life360ConfigFlow.VERSION,
        options=cfg_options(1, verbosity=3),
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    started = asyncio.Event()

    async def request() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
            return "unexpected request completion"
        except asyncio.CancelledError:
            return "request ignored cancellation"

    task = asyncio.create_task(
        coordinator._request(  # noqa: SLF001
            "aid1", request, msg="resistant cancellation"
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
