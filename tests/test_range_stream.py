import io
from pathlib import Path
from unittest.mock import MagicMock

from src.webui_modules.media_service import serve_file_range


def test_serve_file_range_full(tmp_path: Path):
    test_file = tmp_path / "test_media.mp4"
    data = b"0123456789" * 10  # 100 bytes
    test_file.write_bytes(data)

    handler = MagicMock()
    handler.headers = {}
    out_buf = io.BytesIO()
    handler.wfile = out_buf
    headers_sent = {}

    def mock_send_header(k, v):
        headers_sent[k] = v

    handler.send_header.side_effect = mock_send_header

    serve_file_range(handler, test_file)

    handler.send_response.assert_called_with(200)
    assert headers_sent.get("Content-Length") == "100"
    assert headers_sent.get("Accept-Ranges") == "bytes"
    assert out_buf.getvalue() == data


def test_serve_file_range_partial_start_end(tmp_path: Path):
    test_file = tmp_path / "test_audio.mp3"
    data = b"abcdefghij" * 10  # 100 bytes
    test_file.write_bytes(data)

    handler = MagicMock()
    handler.headers = {"Range": "bytes=10-19"}
    out_buf = io.BytesIO()
    handler.wfile = out_buf
    headers_sent = {}

    def mock_send_header(k, v):
        headers_sent[k] = v

    handler.send_header.side_effect = mock_send_header

    serve_file_range(handler, test_file)

    handler.send_response.assert_called_with(206)
    assert headers_sent.get("Content-Length") == "10"
    assert headers_sent.get("Content-Range") == "bytes 10-19/100"
    assert out_buf.getvalue() == data[10:20]


def test_serve_file_range_suffix(tmp_path: Path):
    test_file = tmp_path / "test_video.mp4"
    data = b"0123456789" * 10  # 100 bytes
    test_file.write_bytes(data)

    handler = MagicMock()
    handler.headers = {"Range": "bytes=-15"}
    out_buf = io.BytesIO()
    handler.wfile = out_buf
    headers_sent = {}

    def mock_send_header(k, v):
        headers_sent[k] = v

    handler.send_header.side_effect = mock_send_header

    serve_file_range(handler, test_file)

    handler.send_response.assert_called_with(206)
    assert headers_sent.get("Content-Length") == "15"
    assert headers_sent.get("Content-Range") == "bytes 85-99/100"
    assert out_buf.getvalue() == data[85:100]


def test_serve_file_range_out_of_bounds(tmp_path: Path):
    test_file = tmp_path / "test_video.mp4"
    data = b"0123456789" * 10  # 100 bytes
    test_file.write_bytes(data)

    handler = MagicMock()
    handler.headers = {"Range": "bytes=200-300"}
    headers_sent = {}

    def mock_send_header(k, v):
        headers_sent[k] = v

    handler.send_header.side_effect = mock_send_header

    serve_file_range(handler, test_file)

    handler.send_response.assert_called_with(416)
    assert headers_sent.get("Content-Range") == "bytes */100"


def test_serve_file_range_etag_304(tmp_path: Path):
    test_file = tmp_path / "test_pic.jpg"
    data = b"sample_jpeg_data"
    test_file.write_bytes(data)

    st = test_file.stat()
    valid_etag = f'"{int(st.st_mtime)}-{len(data):x}"'

    handler = MagicMock()
    handler.headers = {"If-None-Match": valid_etag}

    serve_file_range(handler, test_file)

    handler.send_response.assert_called_with(304)
