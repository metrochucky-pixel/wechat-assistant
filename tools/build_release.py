#!/usr/bin/env python3
"""Build a source-only release from a clean Git commit, never from runtime files."""
import hashlib
import io
import json
import re
import subprocess
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(['git',*args],cwd=ROOT)


def allowed(name):
    p=Path(name)
    if name in {'.gitignore','README.md','LICENSE','SKILL.md','.env.example','VERSION','requirements.txt','packaging/wechatbot.env.example'}:return True
    if len(p.parts)==1 and (p.suffix=='.py' or '.example.' in name):return True
    if p.parts[0]=='docs' and p.suffix=='.md':return True
    if p.parts[0]=='tools' and p.suffix=='.py':return True
    if name=='tools/fixtures/history_structure.json':return True
    return p.parts[:2]==('.github','workflows') and p.suffix in ('.yml','.yaml')


def main():
    if git('status','--porcelain').strip():
        raise SystemExit('Commit or remove workspace changes before building a release.')
    commit=git('rev-parse','HEAD').decode().strip()
    version=git('show','HEAD:VERSION').decode().strip()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',version):raise SystemExit('Invalid VERSION')
    names=sorted(n.decode() for n in git('ls-tree','-r','--name-only','-z','HEAD').split(b'\0') if n)
    payload={n:git('show','HEAD:'+n) for n in names if allowed(n)}
    manifest={'version':version,'commit':commit,'files':{n:hashlib.sha256(v).hexdigest() for n,v in payload.items()}}
    payload['MANIFEST.json']=(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()
    prefix='wechatbot-'+version
    output=ROOT/'dist';output.mkdir(exist_ok=True)
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(payload.items()):
            info=zipfile.ZipInfo(prefix+'/'+name,date_time=(2026,1,1,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16
            archive.writestr(info,data)
    content=buffer.getvalue();digest=hashlib.sha256(content).hexdigest()
    dest=output/(prefix+'.zip');dest.write_bytes(content)
    checksum=dest.with_suffix('.zip.sha256')
    checksum.write_text(digest+'  dist/'+dest.name+'\n')
    print(json.dumps({'archive':str(dest),'sha256':digest,'commit':commit,'files':len(payload)},indent=2))


if __name__=='__main__':main()
