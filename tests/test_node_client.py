import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("portal_node_client", ROOT / "deploy/linux/overlays/node_client.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ChunkedResponse:
    def __init__(self, chunks, content_length=None):
        self.chunks = list(chunks)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.closed = False

    def read(self, amount, decode_content=False):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) > amount:
            self.chunks.insert(0, chunk[amount:])
            return chunk[:amount]
        return chunk

    def close(self):
        self.closed = True


class NodeResponseLimitChecks(unittest.TestCase):
    def test_streams_response_within_limit(self):
        response = ChunkedResponse([b"abc", b"def"], content_length=6)
        self.assertEqual(module._read_bounded(response, max_bytes=8), b"abcdef")
        self.assertTrue(response.closed)

    def test_rejects_declared_oversized_response_before_reading(self):
        response = ChunkedResponse([b"x" * 8], content_length=9)
        with self.assertRaises(module.NodeResponseTooLarge):
            module._read_bounded(response, max_bytes=8)
        self.assertEqual(response.chunks, [b"x" * 8])
        self.assertTrue(response.closed)

    def test_rejects_chunked_oversized_response(self):
        response = ChunkedResponse([b"1234", b"5678", b"9"])
        with self.assertRaises(module.NodeResponseTooLarge):
            module._read_bounded(response, max_bytes=8)
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
