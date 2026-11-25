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

    async def test_deepseek_json_request_disables_thinking_only_for_official_endpoint(self):
        original = httpx.AsyncClient
        result = {'specificity': 1, 'evidence_grounding': 0, 'actionability': 1, 'issues': [], 'suggestions': []}
        bodies = []

        def respond(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={'model': ai_service.DEEPSEEK_MODEL,
                                            'choices': [{'message': {'content': json.dumps(result)}}]})

        transport = httpx.MockTransport(respond)
        with patch('ai_service.httpx.AsyncClient', side_effect=lambda **kw: original(transport=transport, **kw)):
            for url in (ai_service.DEEPSEEK_API_URL, 'https://another.example.test/chat/completions'):
                self.assertEqual((await ai_service.request_quality(url, 'synthetic-key', ai_service.DEEPSEEK_MODEL, '{}')).result['quality_level'], 'LOW')
        self.assertEqual(bodies[0]['response_format'], {'type': 'json_object'})
        self.assertEqual(bodies[0]['thinking'], {'type': 'disabled'})
        self.assertNotIn('thinking', bodies[1])
        self.assertIn('"specificity":1', bodies[0]['messages'][0]['content'])
        for code in ('VAGUE_FEEDBACK', 'INSUFFICIENT_EVIDENCE', 'LACK_OF_ACTIONABILITY', 'OTHER'):
            self.assertIn(code, bodies[0]['messages'][0]['content'])

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

    def test_usage_normalization_preserves_unknown_and_zero(self):
        normalize = ai_service.normalize_usage
        self.assertIsNone(normalize(None))
        self.assertIsNone(normalize({'prompt_tokens':True,'completion_tokens':1}))
        self.assertIsNone(normalize({'prompt_tokens':8,'completion_tokens':2,'total_tokens':9}))
        unknown = normalize({'prompt_tokens':8,'completion_tokens':0})
        self.assertEqual(unknown['total_tokens'],8)
        self.assertIsNone(unknown['prompt_cache_hit_tokens'])
        zero = normalize({'prompt_tokens':0,'completion_tokens':0,'total_tokens':0})
        self.assertEqual(zero['prompt_cache_hit_tokens'],0)
        explicit = normalize({'prompt_tokens':8,'completion_tokens':2,'prompt_cache_hit_tokens':3,'prompt_cache_miss_tokens':5})
        self.assertEqual(explicit['prompt_cache_hit_tokens'],3)
        nested = normalize({'prompt_tokens':8,'completion_tokens':2,'prompt_tokens_details':{'cached_tokens':4}})
        self.assertEqual(nested['prompt_cache_miss_tokens'],4)
        broken = normalize({'prompt_tokens':8,'completion_tokens':2,'prompt_cache_hit_tokens':9,'prompt_cache_miss_tokens':0})
        self.assertIsNone(broken['prompt_cache_hit_tokens'])

    async def test_report_estimates_only_known_cost_and_revalidates_diagnostic(self):
        from tests.check_quality_model import check_model, validate_pricing, estimate_cost, QUOTE
        pricing = {'model':'mock-alias','currency':'CNY','as_of':'2026-11-22','source':'synthetic unit-test rates',
                   'input_per_million':'4','cached_input_per_million':'1','output_per_million':'10'}
        with self.assertRaises(ValueError):
            validate_pricing({**pricing,'input_per_million':'NaN'},'mock-alias')
        with self.assertRaises(ValueError):
            validate_pricing(pricing,'different-alias')
        normalized = validate_pricing(pricing,'mock-alias')
        self.assertEqual(estimate_cost(ai_service.normalize_usage({'prompt_tokens':100,'completion_tokens':10}), normalized), (None,'cache_split_unavailable'))
        result = {'specificity':2,'evidence_grounding':2,'actionability':2,'issues':[],'suggestions':[],
                  'diagnostics':[{'type':'INSUFFICIENT_EVIDENCE','field':'strengths','review_quote':QUOTE,
                                  'source_quote':QUOTE,'evidence_index':0,'reason':'检验引用','guidance':'核对说明'},
                                 {'type':'OTHER','field':'strengths','review_quote':'不存在','source_quote':'不存在','evidence_index':0,
                                  'reason':'无法核实','guidance':'请检查'}]}
        original = httpx.AsyncClient
        calls = 0
        def respond(request):
            nonlocal calls
            calls += 1
            if calls==4:
                return httpx.Response(503, json={'error':'synthetic failure'})
            return httpx.Response(200,json={'model':'mock-dated','choices':[{'message':{'content':json.dumps(result)}}],
                    'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120,'prompt_cache_hit_tokens':40,'prompt_cache_miss_tokens':60}})
        with patch('ai_service.httpx.AsyncClient',side_effect=lambda **kw: original(transport=httpx.MockTransport(respond),**kw)):
            report = await check_model('https://example.test','synthetic-key','mock-alias',pricing)
        cost = report['cost_summary']
        self.assertEqual(cost['cost_known_cases'],3)
        self.assertEqual(cost['unknown_cost_cases'],1)
        self.assertEqual(cost['estimated_subtotal_known'],'0.00144')
        self.assertIsNone(cost['estimated_total'])
        self.assertEqual(report['cases'][2]['estimated_cost'],'0.00048')
        self.assertEqual(report['cases'][2]['output']['diagnostics'][0]['location_status'],'LOCATED')
        self.assertEqual(report['cases'][2]['output']['diagnostics'][1]['location_status'],'UNCERTAIN')
        self.assertEqual(report['cases'][3]['cost_status'],'request_error')
        self.assertIsNone(report['cases'][3]['estimated_cost'])
        self.assertNotIn('synthetic-key',json.dumps(report))
