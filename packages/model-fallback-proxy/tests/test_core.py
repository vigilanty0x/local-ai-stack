import unittest

from model_fallback_proxy import probe, route

MODEL = {"name": "m", "order": 1, "status": "healthy", "remaining_requests": 1,
         "context_limit": 10, "capabilities": ["text"]}
REQUEST = {"context_tokens": 1, "capabilities": ["text"]}


class Tests(unittest.TestCase):
    def test_route_and_rejections(self):
        self.assertEqual(route({"request": REQUEST, "models": [MODEL]})["selected"], "m")
        self.assertFalse(route({"request": REQUEST, "models": [{**MODEL, "status": "down"}]})["ok"])
        self.assertFalse(route({"request": {**REQUEST, "context_tokens": 11}, "models": [MODEL]})["ok"])

    def test_no_integer_coercion_and_strict_capabilities(self):
        for value in ("1", True, 1.0):
            self.assertFalse(route({"request": {**REQUEST, "context_tokens": value}, "models": [MODEL]})["ok"])
            self.assertFalse(route({"request": REQUEST, "models": [{**MODEL, "order": value}]})["ok"])
        self.assertFalse(route({"request": {**REQUEST, "capabilities": ["text", "text"]}, "models": [MODEL]})["ok"])
        self.assertFalse(route({"request": {**REQUEST, "capabilities": [[]]}, "models": [MODEL]})["ok"])
        self.assertFalse(route({"request": REQUEST, "models": [{**MODEL, "capabilities": "text"}]})["ok"])

    def test_structured_unique_models(self):
        self.assertFalse(route({"request": REQUEST, "models": [MODEL, MODEL]})["ok"])
        self.assertFalse(route({"request": REQUEST, "models": ["bad"]})["ok"])
        self.assertFalse(route(None)["ok"])

    def test_probe(self):
        self.assertTrue(probe()["ok"])


if __name__ == "__main__":
    unittest.main()
