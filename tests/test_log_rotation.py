from logging.handlers import RotatingFileHandler
from botsdock_connector.log import _RotatingStream


def test_large_output_is_bounded_and_keeps_recent_text(tmp_path):
    path = tmp_path / 'connector.log'
    handler = RotatingFileHandler(path, maxBytes=128, backupCount=2, encoding='utf-8')
    stream = _RotatingStream(handler)
    stream.write('x' * 2000 + 'last output')
    stream.flush()
    assert path.read_text().endswith('last output')
    assert all(file.stat().st_size <= 128 for file in tmp_path.glob('connector.log*'))
    assert len(list(tmp_path.glob('connector.log*'))) <= 3
    handler.close()
