#!/usr/bin/env python3
from __future__ import annotations
import json, time, hmac, hashlib, shutil, subprocess
from pathlib import Path
from urllib.parse import urlencode
import requests, yaml

BASE = Path('/Users/riot91naver.com/Desktop/2026/volky-bot')
ENV = BASE / 'config' / '.env'
CFG = BASE / 'config' / 'scalping.yaml'
STATE = BASE / 'papertrade' / 'scalp_live_state.json'
OUT = BASE / 'dashboard' / 'data' / 'status.json'
COIN_SITE = Path('/Users/riot91naver.com/Desktop/coin-site')
COIN_OUT = COIN_SITE / 'data' / 'status.json'


def load_env(p: Path):
    d = {}
    if not p.exists():
        return d
    for line in p.read_text(encoding='utf-8').splitlines():
        if '=' in line and not line.strip().startswith('#'):
            k,v = line.split('=',1); d[k.strip()] = v.strip()
    return d


def signed_get(base,key,sec,endpoint,params):
    p=dict(params)
    p['timestamp']=int(time.time()*1000)
    p['recvWindow']=5000
    qs=urlencode(p)
    sig=hmac.new(sec.encode(),qs.encode(),hashlib.sha256).hexdigest()
    url=f"{base}{endpoint}?{qs}&signature={sig}"
    r=requests.get(url,headers={'X-MBX-APIKEY':key},timeout=8)
    r.raise_for_status(); return r.json()


def main():
    env=load_env(ENV)
    cfg=yaml.safe_load(CFG.read_text(encoding='utf-8')) if CFG.exists() else {}
    base=env.get('BASE_URL','https://testnet.binancefuture.com')
    key=env.get('API_KEY',''); sec=env.get('API_SECRET','')

    positions=[]
    unreal=0.0
    realized=0.0
    comm=0.0
    real_order = bool(cfg.get('real_order', True))

    if key and sec and real_order:
        try:
            arr=signed_get(base,key,sec,'/fapi/v2/positionRisk',{})
            for p in arr:
                amt=float(p.get('positionAmt',0) or 0)
                if abs(amt)<=0: continue
                u=float(p.get('unRealizedProfit',0) or 0)
                unreal += u
                positions.append({
                    'symbol': p['symbol'],
                    'side': 'LONG' if amt>0 else 'SHORT',
                    'qty': abs(amt),
                    'entry': float(p.get('entryPrice',0) or 0),
                    'mark': float(p.get('markPrice',0) or 0),
                    'unrealized_pnl': u,
                    'order_id': None
                })

            # today realized+commission
            import datetime
            start=datetime.datetime.now(datetime.timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
            st=int(start.timestamp()*1000)
            real=signed_get(base,key,sec,'/fapi/v1/income',{'incomeType':'REALIZED_PNL','startTime':st,'limit':200})
            com=signed_get(base,key,sec,'/fapi/v1/income',{'incomeType':'COMMISSION','startTime':st,'limit':200})
            realized=sum(float(x.get('income',0) or 0) for x in real)
            comm=sum(float(x.get('income',0) or 0) for x in com)
        except Exception:
            # API 인증 실패/제한 시에도 대시보드 파일은 계속 갱신
            positions=[]
            unreal=0.0
            realized=0.0
            comm=0.0

    # 페이퍼 모드에서는 로컬 상태 파일을 표시 소스로 사용
    if not real_order and STATE.exists():
        try:
            st = json.loads(STATE.read_text(encoding='utf-8'))
            for sym, v in st.items():
                if not isinstance(v, dict):
                    continue
                pos = v.get('position')
                if pos not in ('LONG','SHORT'):
                    continue
                positions.append({
                    'symbol': sym,
                    'side': pos,
                    'qty': float(v.get('qty',0) or 0),
                    'entry': float(v.get('entryApprox', v.get('entry', 0)) or 0),
                    'mark': 0.0,
                    'unrealized_pnl': 0.0,
                    'order_id': v.get('orderId')
                })
        except Exception:
            pass

    data={
      'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
      'config': {
        'margin_mode': cfg.get('margin_mode','ISOLATED'),
        'leverage': cfg.get('leverage',3),
        'max_positions': cfg.get('max_positions',6),
        'order_notional_usdt': cfg.get('order_notional_usdt',40)
      },
      'pnl': {
        'realized_gross': realized,
        'commission': comm,
        'realized_net': realized + comm,
        'unrealized': unreal,
        'total': realized + comm + unreal
      },
      'positions': positions
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data,ensure_ascii=False,indent=2)
    OUT.write_text(payload,encoding='utf-8')

    # coin-site도 함께 동기화(가능할 때만)
    try:
      COIN_OUT.parent.mkdir(parents=True, exist_ok=True)
      changed = (not COIN_OUT.exists()) or (COIN_OUT.read_text(encoding='utf-8') != payload)
      if changed:
        COIN_OUT.write_text(payload, encoding='utf-8')
        subprocess.run(['git','-C',str(COIN_SITE),'add','data/status.json'], check=False)
        diff = subprocess.run(['git','-C',str(COIN_SITE),'diff','--cached','--quiet'])
        if diff.returncode != 0:
          subprocess.run(['git','-C',str(COIN_SITE),'commit','-m','chore: sync live status'], check=False)
          subprocess.run(['git','-C',str(COIN_SITE),'push'], check=False)
    except Exception:
      pass

if __name__ == '__main__':
    main()
