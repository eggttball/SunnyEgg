#!/usr/bin/env python3
"""Taipei scheduler; once/simulation is the same batch path as a daily trigger."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

from daily import run_daily

ROOT = Path(__file__).resolve().parents[1]
TAIPEI = ZoneInfo('Asia/Taipei')

def load_schedule(root):
    with (Path(root)/'config/job.json').open(encoding='utf-8') as handle:
        config=json.load(handle)
    if config.get('version')!=1 or config.get('media')!='photos' or config.get('schedule')!={'cron':'0 20 * * *','timeZone':'Asia/Taipei'}:
        raise ValueError('排程設定需為台灣時間每日 20:00，僅處理照片')
    return config

def next_trigger(now=None):
    clock=now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError('排程時間必須包含時區')
    local=clock.astimezone(TAIPEI)
    scheduled=local.replace(hour=20,minute=0,second=0,microsecond=0)
    return scheduled+timedelta(days=1) if local>scheduled else scheduled

def trigger_once(root=ROOT, discovery_file=None, simulate=False, env_file=None):
    config=load_schedule(root)
    return run_daily(Path(root),discovery_file=discovery_file,simulate=simulate,
                     threshold=config.get('threshold',0.5),env_file=env_file)

def main(argv=None):
    parser=argparse.ArgumentParser(description='SunnyEgg：每天台灣時間20:00執行照片流程')
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--once',action='store_true',help='立即執行與每日排程相同的批次流程')
    parser.add_argument('--simulate',action='store_true',help='立即重播發現階段；API下載與照片辨識實際執行，不按讚')
    parser.add_argument('--discovery-file',type=Path)
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--show-next',action='store_true')
    args=parser.parse_args(argv)
    try:
        load_schedule(args.root)
        if args.show_next:
            print(json.dumps({'schedule':{'cron':'0 20 * * *','timeZone':'Asia/Taipei'},'nextRunAt':next_trigger().isoformat()},ensure_ascii=False));return 0
        if args.simulate and not args.discovery_file:
            parser.error('--simulate 需要 --discovery-file；不會把重播當作即時掃描')
        if args.discovery_file and not args.simulate:
            parser.error('--discovery-file 僅用於 --simulate')
        if args.once or args.simulate:
            result=trigger_once(args.root,args.discovery_file,args.simulate,args.env_file)
            print(json.dumps(result,ensure_ascii=False))
            return 0 if result.get('state')=='complete' else 2
        while True:
            scheduled=next_trigger()
            print('下次照片觸發：'+scheduled.isoformat(),flush=True)
            while (scheduled-datetime.now(TAIPEI)).total_seconds()>0:
                time.sleep(min(30,(scheduled-datetime.now(TAIPEI)).total_seconds()))
            result=trigger_once(args.root,env_file=args.env_file)
            print(json.dumps(result,ensure_ascii=False),flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print('照片排程未完成（{}）；請檢查設定或批次報告'.format(type(error).__name__),file=sys.stderr)
        return 2

if __name__=='__main__':
    sys.exit(main())
