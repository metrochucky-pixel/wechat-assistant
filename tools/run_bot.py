#!/usr/bin/env python3
"""Explicit launch entry: defaults to shadow mode; --send enables WeChat delivery."""
import argparse
import sys
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--send',action='store_true',help='Enable real WeChat replies on the configured phone')
    args=parser.parse_args()
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    import wechat_autoreply as bot
    bot.DRY_RUN=args.send is not True
    bot.main()


if __name__=='__main__':main()
