import asyncio
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

    async def test_quality_report_records_model_version_and_timeout(self):
        from tests.check_quality_model import check_model

        original = httpx.AsyncClient
        requests = []
        result = {'specificity': 2, 'evidence_grounding': 2, 'actionability': 1, 'issues': [], 'suggestions': []}

        async def respond(request):
            requests.append(json.loads(request.content))
            if len(requests) == 4:
                await asyncio.sleep(1)
            return httpx.Response(200, json={'model': 'mock-dated-version',
                                            'choices': [{'message': {'content': json.dumps(result)}}]})

        transport = httpx.MockTransport(respond)
        with patch('ai_service.MODEL_TIMEOUT_SECONDS', 0.05), \
             patch('ai_service.httpx.AsyncClient', side_effect=lambda **kw: original(transport=transport, **kw)):
            report = await check_model('https://example.test', 'temporary-mock-key', 'mock-alias')
        self.assertEqual(report['requested_model'], 'mock-alias')
        self.assertEqual([row['expected_level'] for row in report['cases']], ['LOW', 'MEDIUM', 'HIGH', 'HIGH'])
        self.assertEqual(report['cases'][0]['reported_model'], 'mock-dated-version')
        self.assertEqual(report['cases'][0]['output']['quality_level'], 'HIGH')
        self.assertEqual(report['cases'][-1]['error_type'], 'TimeoutError')
        self.assertIsNone(report['cases'][-1]['output'])
        self.assertTrue(all(row['human_assessment'] is None and row['elapsed_ms'] >= 0 for row in report['cases']))
        self.assertNotIn('temporary-mock-key', json.dumps(report))
        for request in requests:
            self.assertEqual(request['messages'][0]['role'], 'system')
            self.assertEqual(request['messages'][1]['role'], 'user')
        self.assertIn('附录', json.loads(requests[-1]['messages'][1]['content'])['submission_excerpt'])
