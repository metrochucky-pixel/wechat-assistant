"""Offline regressions: load pure routing functions only; never touch phone or API."""
import ast
from pathlib import Path
import unittest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot_performance as perf
import model_router

SOURCE = Path(__file__).resolve().parents[1] / 'wechat_autoreply.py'

def functions():
    tree = ast.parse(SOURCE.read_text())
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name in ('deep_answer', 'focused_deep_answer', 'requires_current_sources')]
    ns = {"perf": perf, "model_router":model_router,"re": __import__("re"), "WebAnswer": type("WebAnswer", (str,), {}), "_grok_web": lambda *a: ""}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), 'exec'), ns)
    return ns

class FocusedQA(unittest.TestCase):
    def test_school_question_survives_drinking_chatter(self):
        ns = functions(); calls = []
        ns['deep_answer'] = lambda q, context: calls.append((q, context)) or '学校答案'
        d = {'last_customer_msg': '赵总说你们基本没喝',
             'question_to_answer': '介绍新加坡教学资源，有什么好的学校和专业？',
             'question_context': '朋友考虑送孩子去读书，年级和预算未知'}
        self.assertEqual(ns['focused_deep_answer'](d, '群聊(5)'), '学校答案')
        self.assertIn('学校和专业', calls[0][0]); self.assertNotIn('没喝', str(calls))
        self.assertEqual(d['_deep_question'], d['question_to_answer'])

    def test_missing_question_does_not_call_api_with_chatter(self):
        ns = functions()
        ns['deep_answer'] = lambda *a, **k: self.fail('Must not call API')
        for q in (None, '', '  ', False, ['school']):
            d = {'question_to_answer': q, 'last_customer_msg': '昨天没喝'}
            self.assertIn('没读完整', ns['focused_deep_answer'](d, '群聊(5)'))

    def test_failed_api_does_not_reuse_unrelated_draft(self):
        ns = functions(); ns['deep_answer'] = lambda *a, **k: ''
        d = {'question_to_answer': '学校和专业有哪些？', 'reply': '昨天喝少了'}
        self.assertIn('没成功', ns['focused_deep_answer'](d, '群聊(5)'))

    def test_web_failure_does_not_publish_unverified_numbers(self):
        ns = functions(); ns['_kimi_deep'] = lambda *a: '学费123456 [NEED_WEB]'
        ns['_claude_deep'] = lambda *a: ''
        result = ns['deep_answer']('今年学费')
        self.assertEqual(result, '')  # Job must remain pending; do not send failed lookup as completion.

    def test_verified_web_and_stable_answers(self):
        ns = functions(); ns['_kimi_deep'] = lambda *a: '草稿 [NEED_WEB]'
        ns['_claude_deep'] = lambda *a: '联网结果与来源'
        self.assertEqual(ns['deep_answer']('政策'), '联网结果与来源')
        ns['_kimi_deep'] = lambda *a: '稳定知识'
        ns['_claude_deep'] = lambda *a: self.fail('No search needed')
        self.assertEqual(ns['deep_answer']('概念'), '稳定知识')

if __name__ == '__main__':
    unittest.main()
