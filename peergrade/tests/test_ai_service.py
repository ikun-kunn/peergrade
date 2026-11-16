import json
import unittest
from unittest.mock import patch

import httpx
import ai_service


class AIServiceTests(unittest.IsolatedAsyncioTestCase):
    async def call_with(self, response):
        original = httpx.AsyncClient
        transport = httpx.MockTransport(lambda request: response)
        with patch('ai_service.httpx.AsyncClient', side_effect=lambda **kw: original(transport=transport, **kw)):
            return await ai_service.evaluate_quality('https://example.test', 'local-test-key', 'mock', '{}')

    async def test_server_calculates_level(self):
        result = {'specificity':2, 'evidence_grounding':2, 'actionability':1,
                  'quality_level':'LOW', 'issues':[], 'suggestions':['补充案例']}
        response = httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(result)}}]})
        self.assertEqual((await self.call_with(response))['quality_level'], 'HIGH')

    async def test_rejects_invalid_and_oversized_model_output(self):
        result = {'specificity':True, 'evidence_grounding':2, 'actionability':1, 'issues':[], 'suggestions':[]}
        response = httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(result)}}]})
        with self.assertRaises(ValueError):
            await self.call_with(response)
        with self.assertRaises(ValueError):
            await self.call_with(httpx.Response(200, content=b'x'*32769))
