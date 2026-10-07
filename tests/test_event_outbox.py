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
