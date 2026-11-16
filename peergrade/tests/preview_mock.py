"""Local UI test server with synthetic model output; never use for deployment."""
import os
os.environ['DATABASE_URL'] = 'sqlite:///./preview_ui.db'
os.environ['AI_API_KEY'] = 'local-fixture'
os.environ['AI_API_URL'] = 'https://example.invalid'
os.environ['AI_MODEL'] = 'local-fixture'

import app
import uvicorn


async def mock_quality(*args):
    return {'specificity': 1, 'evidence_grounding': 0, 'actionability': 1,
            'quality_level': 'LOW', 'issues': [], 'suggestions': ['补充原文依据']}


app.evaluate_quality = mock_quality
uvicorn.run(app.app, host='127.0.0.1', port=8766)
