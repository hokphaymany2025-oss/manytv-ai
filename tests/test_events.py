"""Tests for backend/core/events.py -- the in-process pub/sub bus SSE routes
(backend/api/routes/storyboard.py) subscribe to and JobStore publishes to.

Uses asyncio.run() directly in plain `def test_...` functions, matching this
repo's existing test style -- no async test plugin.
"""

import asyncio

from backend.core.events import EventBus, JobEvent


def test_subscriber_receives_published_event_for_its_job_id():
    bus = EventBus()

    async def scenario():
        queue = bus.subscribe("job-1")
        bus.publish("job-1", "job_updated")
        return await queue.get()

    event = asyncio.run(scenario())

    assert event == JobEvent(job_id="job-1", event_type="job_updated")


def test_subscriber_does_not_receive_events_for_a_different_job_id():
    bus = EventBus()

    async def scenario():
        queue = bus.subscribe("job-1")
        bus.publish("job-2", "job_updated")
        return queue.empty()

    assert asyncio.run(scenario()) is True


def test_none_subscriber_receives_events_for_every_job_id():
    bus = EventBus()

    async def scenario():
        queue = bus.subscribe(None)
        bus.publish("job-1", "job_created")
        bus.publish("job-2", "job_updated")
        first = await queue.get()
        second = await queue.get()
        return first, second

    first, second = asyncio.run(scenario())

    assert first == JobEvent(job_id="job-1", event_type="job_created")
    assert second == JobEvent(job_id="job-2", event_type="job_updated")


def test_multiple_subscribers_to_the_same_job_id_all_receive_the_event():
    bus = EventBus()

    async def scenario():
        queue_a = bus.subscribe("job-1")
        queue_b = bus.subscribe("job-1")
        bus.publish("job-1", "job_updated")
        return await queue_a.get(), await queue_b.get()

    a, b = asyncio.run(scenario())

    assert a == b == JobEvent(job_id="job-1", event_type="job_updated")


def test_unsubscribe_stops_further_delivery():
    bus = EventBus()

    async def scenario():
        queue = bus.subscribe("job-1")
        bus.unsubscribe("job-1", queue)
        bus.publish("job-1", "job_updated")
        return queue.empty()

    assert asyncio.run(scenario()) is True


def test_full_queue_drops_oldest_event_rather_than_blocking_publish():
    bus = EventBus(maxsize=2)

    async def scenario():
        queue = bus.subscribe("job-1")
        bus.publish("job-1", "job_updated")  # dropped once the third publish arrives
        bus.publish("job-1", "job_updated")
        bus.publish("job-1", "log_added")
        remaining = []
        while not queue.empty():
            remaining.append(queue.get_nowait())
        return remaining

    remaining = asyncio.run(scenario())

    assert len(remaining) == 2
    assert remaining[-1] == JobEvent(job_id="job-1", event_type="log_added")


def test_publish_with_no_subscribers_does_not_raise():
    bus = EventBus()
    bus.publish("job-1", "job_updated")  # no subscribers at all -- must be a no-op, not an error
