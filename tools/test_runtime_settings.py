import os,sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import runtime_settings as settings
from conversation_planner import clean_messages


class RuntimeSettings(unittest.TestCase):
    def message(self,text,**kwargs):
        return dict(sender='甲',text=text,quote='',self=False,direct_mention=True,certain=True,**kwargs)
    def test_configured_nickname_is_required(self):
        with patch.dict(os.environ,{'WECHAT_SELF_NAME':'测试账号'}):
            self.assertEqual(len(clean_messages([self.message('@测试账号 问题')])),1)
            with self.assertRaises(ValueError):clean_messages([self.message('@Bot Example 问题')])
    def test_missing_nickname_cannot_authorize(self):
        with patch.dict(os.environ,{'WECHAT_SELF_NAME':''}):
            with self.assertRaises(ValueError):clean_messages([self.message('@任何人 问题')])
    def test_quote_does_not_authorize(self):
        m=self.message('普通消息');m['quote']='@Bot Example 旧问题'
        with self.assertRaises(ValueError):clean_messages([m])
    def test_configured_group_exact_match(self):
        with patch.dict(os.environ,{'WECHAT_MENTION_ONLY_GROUPS':'甲群|Beta'}):
            self.assertTrue(settings.mention_only('甲群(12)'))
            self.assertTrue(settings.mention_only('beta'))
            self.assertFalse(settings.mention_only('甲群二'))
            self.assertFalse(settings.mention_only('MentionOnly'))
    def test_discovery_alias_only_changes_configured_name(self):
        with patch.dict(os.environ,{'WECHAT_DISCOVERY_ALIASES':'{"错名":"正确群名"}'}):
            self.assertEqual(settings.discovery_name('错名'),'正确群名')
            self.assertEqual(settings.discovery_name('其他'),'其他')
    def test_invalid_alias_configuration_rejected(self):
        for value in ['[]','{"群":true}','broken']:
            with patch.dict(os.environ,{'WECHAT_DISCOVERY_ALIASES':value}):
                with self.assertRaises(ValueError):settings.discovery_name('群')


if __name__=='__main__':unittest.main()
