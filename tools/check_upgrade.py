#!/usr/bin/env python3
"""Offline upgrade gate: deterministic safety suites plus sanitized history replay."""
import subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
suites=['background','image_review','technical_recovery','reliability','round1_performance',
        'focused_qa','scheduler','turn_integration','web_evidence','owner_review','conversation_turns','round4','round5','owner_memory','boundaries','natural_feedback','discovery_scope','runtime_settings']
for name in suites:
    result=subprocess.run([sys.executable,str(root/'tools'/('test_'+name+'.py'))],capture_output=True,text=True,cwd=root)
    print(name+': '+('PASS' if result.returncode==0 else 'FAIL'),flush=True)
    if result.returncode:
        print(result.stdout+result.stderr);sys.exit(result.returncode)
result=subprocess.run([sys.executable,str(root/'tools/replay_history.py'),'--fixture',str(root/'tools/fixtures/history_structure.json')],cwd=root)
sys.exit(result.returncode)
