"""Deployment-specific routing. No account names or real group names in source."""
import json
import os
from bot_reliability import conversation_key


def mention_only(name):
    configured=os.environ.get('WECHAT_MENTION_ONLY_GROUPS','MentionOnly')
    return conversation_key(name) in {conversation_key(n) for n in configured.split('|') if n.strip()}


def discovery_name(name):
    aliases=json.loads(os.environ.get('WECHAT_DISCOVERY_ALIASES','{}'))
    if not isinstance(aliases,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in aliases.items()):
        raise ValueError('WECHAT_DISCOVERY_ALIASES must be a string-to-string JSON object')
    return aliases.get(name,name)


def self_name():
    return os.environ.get("WECHAT_SELF_NAME", "Bot Example").strip()
