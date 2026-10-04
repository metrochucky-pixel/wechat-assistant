"""Search completion needs real tool evidence plus citations, not merely confident model text."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace as S
import os
import atexit
import tempfile
_import_state = tempfile.TemporaryDirectory(prefix='wechatbot-test-')
atexit.register(_import_state.cleanup)
os.environ['WECHATBOT_OUTBOX'] = str(Path(_import_state.name) / 'import.sqlite3')
import wechat_autoreply as b
from bot_reliability import WebAnswer

class SearchEvidence(unittest.TestCase):
    def response(self, searched=True, citations=True, status='completed'):
        return S(status=status,output=([S(type='web_search_call',status='completed')] if searched else []) +
            [S(type='message',content=[S(type='output_text',text='学校官网说明',annotations=[S(type='url_citation',url='https://www.nus.edu.sg/oam')] if citations else [])])])

    def call(self,response):
        with patch.object(b,'_api_create',return_value=response), patch.object(b,'load_persona',return_value='原有聊天风格'), patch.dict(b.os.environ,{'XAI_API_KEY':'test-no-network'}):
            return b._grok_web('学校招生','本科')

    def test_text_without_search_is_rejected(self):
        self.assertEqual(self.call(self.response(searched=False)),'')

    def test_search_without_citations_is_rejected(self):
        self.assertEqual(self.call(self.response(citations=False)),'')

    def test_incomplete_search_is_rejected(self):
        self.assertEqual(self.call(self.response(status='incomplete')),'')

    def test_complete_search_preserves_sources_and_time(self):
        answer=self.call(self.response())
        self.assertIsInstance(answer,WebAnswer)
        self.assertIn('https://www.nus.edu.sg/oam',answer)
        self.assertEqual(answer.sources,['https://www.nus.edu.sg/oam'])
        self.assertGreater(answer.verified_at,0)

    def test_current_policy_forces_search_even_if_kimi_forgets_marker(self):
        with patch.object(b,'_kimi_deep',return_value='未经验证的旧数字123456'), patch.object(b,'_claude_deep',return_value=''), patch.object(b,'_grok_web',return_value='') as search:
            self.assertEqual(b.deep_answer('今年新加坡学费和招生政策'), '')
            search.assert_called_once()

    def test_secondary_search_recovers_claude_failure(self):
        with patch.object(b,'_kimi_deep',return_value='[NEED_WEB]'), patch.object(b,'_claude_deep',return_value=''), patch.object(b,'_grok_web',return_value=WebAnswer('学校答案',['https://www.nus.edu.sg/oam'],'grok-search')):
            decision={'question_to_answer':'学校招生'}
            b.focused_deep_answer(decision,'朋友群')
            self.assertEqual(decision['_deep_status'],'returned')
            self.assertEqual(decision['_sources'],['https://www.nus.edu.sg/oam'])

if __name__=='__main__': unittest.main()
