"""One bounded computation. No phone operations, notifications, or job mutations."""
import json
import os
from pathlib import Path
import sys
from PIL import Image
import model_router
import image_fact_cache
import natural_preferences


@natural_preferences.scoped(lambda bot,request:request['display'])
def compute(bot, request):
    def restore(value):
        if isinstance(value,dict) and set(value)=={'image_path'}:
            with Image.open(value['image_path']) as im:return im.convert('RGB').copy()
        if isinstance(value,dict):return {k:restore(v) for k,v in value.items()}
        if isinstance(value,list):return [restore(v) for v in value]
        return value
    inputs=restore(request['inputs']);d=request['payload'];conv=request['display']
    phase=request['phase']
    if phase=='draft':
        draft=None
        if inputs.get('simple'):
            draft=bot._simple_draft(d,inputs['snapshot'],conv)
            if draft is None:return {'draft':{'_needs_full_route':True}}
        if draft is None:
            draft=bot.validate_decision(bot.read_and_decide([inputs['screen']],memory_text=bot.task_memory(conv,d),
                gag_text='',todo_text=bot.get_todos(conv),
                target_request={'question':d['question_to_answer'],'context':d['question_context']}))
        return {'draft':draft}
    if phase=='knowledge':
        answer=bot.focused_deep_answer(d,conv)
        if d.get('_deep_status')!='returned':raise ValueError('knowledge unavailable')
        return {'reply':str(answer),'evidence':{k:d[k] for k in ('_sources','_verified_at','_search_provider','_deep_status') if k in d}}
    if phase=='image_facts':
        observed,cache_hit=image_fact_cache.analyze(bot.OUTBOX,bot._guard_read,inputs['frames'],inputs['request'],inputs.get('zoom'),conv)
        if not observed:raise ValueError('image facts insufficient')
        reply=bot._compose_image_facts(inputs['request'],observed)
        if not reply:raise ValueError('image reply unavailable')
        return {'review':dict(inputs['metadata'],facts=observed,reply=reply,cache_hit=cache_hit)}
    if phase=='generate_image':
        path=bot.gen_image(d.get('image_prompt') or '')
        if not path:raise ValueError('image generation unavailable')
        return {'image_path':path}
    raise ValueError('unknown work phase')


def main():
    os.environ['WECHATBOT_BACKGROUND_WORKER']='1'
    import threading,time
    parent=int(os.environ.get('WECHATBOT_PARENT_PID',os.getppid()))
    def parent_watch():
        while os.getppid()==parent:time.sleep(.25)
        os._exit(70)
    threading.Thread(target=parent_watch,daemon=True).start()
    import wechat_autoreply as bot
    def prohibited(*a,**k):raise RuntimeError('background worker cannot operate phone or notify')
    for name in ('adb','screencap','tap','back','push_to_gallery','send_reply','send_image_from_gallery','notify_feishu'):
        setattr(bot,name,prohibited)
    request=json.loads(Path(sys.argv[1]).read_text())
    try:
        with model_router.task(request['route']):data=compute(bot,request)
        result={'ok':True,'data':data}
    except BaseException as exc:result={'ok':False,'data':{},'error':type(exc).__name__}
    output=Path(sys.argv[2]);temp=output.with_suffix('.tmp')
    temp.write_text(json.dumps(result,ensure_ascii=False));os.chmod(temp,0o600);os.replace(temp,output)


if __name__=='__main__':main()
