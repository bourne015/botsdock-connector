from pathlib import Path
from botsdock_connector.event_outbox import EventOutbox


def test_events_survive_restart_until_acknowledged(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    first = EventOutbox('https://server', 'machine')
    first.put_nowait({'type': 'connector.event', 'event_type': 'turn.completed'})
    event = first.get_nowait()
    assert event['event_id']
    first.close()
    second = EventOutbox('https://server', 'machine')
    assert second.pending() == [event]
    second.acknowledge({'type': 'connector.event_ack', 'event': {'id': event['event_id']}})
    assert second.pending() == []
    second.close()


def test_transients_are_not_persisted_and_machines_are_isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    first = EventOutbox('https://server', 'first')
    first.put_nowait({'type': 'connector.transient'})
    assert first.pending() == []
    first.put_nowait({'type': 'connector.event'})
    other = EventOutbox('https://server', 'other')
    assert other.pending() == []
    first.close()
    other.close()


async def test_failed_socket_send_keeps_the_event_for_reconnect(monkeypatch, tmp_path):
    import pytest
    from botsdock_connector.session import outbound_writer
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    queue = EventOutbox('server', 'machine')
    queue.put_nowait({'type': 'connector.event', 'event_type': 'assistant.message'})
    class BrokenSocket:
        async def send(self, data):
            raise ConnectionError('disconnected')
    with pytest.raises(ConnectionError):
        await outbound_writer(BrokenSocket(), queue)
    assert len(queue.pending()) == 1
    queue.close()


async def test_delivery_ack_clears_original_id_when_server_merges_events(monkeypatch, tmp_path):
    from botsdock_connector.session import message_loop
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    queue = EventOutbox('server', 'machine')
    queue.put_nowait({'type': 'connector.event', 'event_id': 'delivery'})
    class Socket:
        async def __aiter__(self):
            import json
            yield json.dumps({'type': 'connector.event_ack', 'event_id': 'delivery',
                              'event': {'id': 'merged-event'}})
    async def handler(message):
        return None
    await message_loop(Socket(), handler=handler, outbound=queue)
    assert queue.pending() == []
    queue.close()


async def test_full_memory_queue_does_not_drop_durable_events(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    queue = EventOutbox('server', 'machine')
    for _ in range(queue.maxsize):
        queue.put_nowait({'type': 'connector.transient'})
    await queue.put({'type': 'connector.event', 'event_type': 'turn.completed'})
    assert len(queue.pending()) == 1
    assert queue.qsize() == queue.maxsize
    queue.close()
