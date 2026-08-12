"""Candidate V9: volatility expansion / squeeze breakout comparison."""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
from collections import Counter,defaultdict
from decimal import Decimal
from pathlib import Path

from tradingview_mcp.core.backtest.comparison_candidate_v1 import build_research_v2_candidate_chain_indexed
from tradingview_mcp.core.backtest.comparison_execution_v1 import simulate_neutral_signal
from tradingview_mcp.core.backtest.comparison_history_index_v1 import HistoricalWindowIndex
from tradingview_mcp.core.backtest.comparison_history_v1 import neutral_signals_from_research
from tradingview_mcp.core.backtest.comparison_lifecycle_v1 import SingleSymbolLifecycleGate
from tradingview_mcp.core.backtest.comparison_v1 import DEFAULT_SCAN_HOURS,decision_times,summarize_observations
from tradingview_mcp.core.backtest.comparison_volatility_expansion_v1 import evaluate_volatility_expansion
from tradingview_mcp.core.backtest.extended_history_loader_v1 import load_extended_bundle
from tradingview_mcp.core.backtest.extended_history_v1 import DEFAULT_CACHE_ROOT,write_json_cache
UTC=dt.timezone.utc; MIN_FUTURE_EXECUTION=dt.timedelta(hours=12)
SCHEDULES={"CURRENT_DAYTIME_2H_07_21":tuple(DEFAULT_SCAN_HOURS),"FULL_24H_2H":tuple(range(0,24,2)),"FULL_24H_1H":tuple(range(24))}
VARIANTS={"MEAN_REVERSION_ONLY":{"mean_reversion"},"VOLATILITY_EXPANSION_ONLY":{"volatility_expansion"},"MEAN_REVERSION_PLUS_VOLATILITY_EXPANSION":{"mean_reversion","volatility_expansion"}}

def _dt(v):
 o=dt.datetime.fromisoformat(v); return o.astimezone(UTC)
def _jsonable(v):
 if isinstance(v,Decimal): return str(v)
 if isinstance(v,(dt.datetime,dt.date)): return v.isoformat()
 if isinstance(v,dict): return {str(k):_jsonable(x) for k,x in v.items()}
 if isinstance(v,(list,tuple)): return [_jsonable(x) for x in v]
 return v
def _future(c,t,cut): return c[bisect.bisect_left(t,cut):]
def _by_setup(rows):
 g=defaultdict(list)
 for r in rows:g[r.setup_name].append(r)
 return {k:summarize_observations(v) for k,v in sorted(g.items())}
def _life(events,allowed,end):
 eligible=[e for e in events if e['setup'] in allowed]; conflicts=set()
 if len(allowed)>1:
  g=defaultdict(set)
  for e in eligible:g[(e['symbol'],e['decision_time'])].add(e['setup'])
  conflicts={k for k,v in g.items() if len(v)>1}; eligible=[e for e in eligible if (e['symbol'],e['decision_time']) not in conflicts]
 gate=SingleSymbolLifecycleGate(); sub=[]; sup=Counter()
 for e in eligible:
  if not gate.can_submit(e['symbol'],e['decision_time']): sup[e['setup']]+=1; continue
  sub.append(e); gate.occupy_from_order(e['symbol'],e['order'],fallback_end=end)
 rows=[e['observation'] for e in sub]
 return {'lifecycle':summarize_observations(rows),'by_setup':_by_setup(rows),'submitted_by_setup':dict(Counter(e['setup'] for e in sub)),'suppressed_busy_by_setup':dict(sup),'same_scan_cross_setup_conflicts_excluded':len(conflicts),'gate':gate.diagnostics()}
def _run(name,hours,requested,successful,start,end):
 ctx=list(requested)
 if 'BTCUSDT' not in ctx and 'BTCUSDT' in successful:ctx=['BTCUSDT']+ctx
 elif 'BTCUSDT' in ctx:ctx=['BTCUSDT']+[s for s in ctx if s!='BTCUSDT']
 bundles={s:load_extended_bundle(Path(successful[s]['manifest']['cache_dir'])) for s in ctx}; indexes={s:HistoricalWindowIndex(b.inputs) for s,b in bundles.items()}; candles={s:list(b.inputs.candles_1m) for s,b in bundles.items()}; times={s:[c['open_time'] for c in candles[s]] for s in bundles}; states={s:None for s in ctx}; cuts=decision_times(start,end,hours=hours)
 events=[]; funnel=Counter(); w3=Counter(); w4=Counter(); br=Counter(); bd=Counter()
 for cut in cuts:
  btc=None; chains={}
  for s in ctx:
   ch=build_research_v2_candidate_chain_indexed(s,indexes[s],cut,btc_regime=btc,previous_state=states[s]);states[s]=ch.get('_state');chains[s]=ch
   if s=='BTCUSDT':btc=ch['regime'].get('primary_regime')
  for s in requested:
   ch=chains[s]; fut=_future(candles[s],times[s],cut)
   for sig in neutral_signals_from_research(ch,approved_only=True):
    if sig.setup_name=='mean_reversion':
     r=simulate_neutral_signal(sig,fut);events.append({'symbol':s,'decision_time':sig.decision_time,'setup':'mean_reversion','observation':r.observation,'order':r.order})
   x=evaluate_volatility_expansion(ch,btc_regime=btc); setup=x['setup']; d=setup.get('direction')
   if d in {'LONG','SHORT'}:
    funnel['active_setup']+=1;br[str(ch['regime'].get('primary_regime'))]+=1;bd[d]+=1;w3[str((x.get('confirmation') or {}).get('status'))]+=1;w4[str((x.get('risk') or {}).get('decision'))]+=1
   else:funnel['no_setup']+=1
   sig=x.get('signal')
   if sig:
    funnel['approved_signal']+=1;r=simulate_neutral_signal(sig,fut);events.append({'symbol':s,'decision_time':sig.decision_time,'setup':'volatility_expansion','observation':r.observation,'order':r.order})
 return {'schedule':{'name':name,'hours':list(hours),'cutoffs_per_symbol':len(cuts),'symbols':len(requested),'total_symbol_decision_points':len(cuts)*len(requested)},'volatility_funnel':dict(funnel),'volatility_active_by_w1_regime':dict(br),'volatility_active_by_direction':dict(bd),'volatility_w3':dict(w3),'volatility_w4':dict(w4),'variants':{k:_life(events,v,end) for k,v in VARIANTS.items()}}
def main(args):
 root=Path(args.cache_root); back=json.loads((root/'backfill_summary.json').read_text()); successful={s:r for s,r in (back.get('results') or {}).items() if r.get('status')=='OK' and r.get('manifest',{}).get('cache_dir')}; requested=[s.strip().upper() for s in args.symbols.split(',')] if args.symbols else list(successful); win=back['window']; start=_dt(win['evaluation_start']); end=_dt(win['evaluation_end'])-MIN_FUTURE_EXECUTION
 schedules=SCHEDULES if not args.schedule else {args.schedule:SCHEDULES[args.schedule]}
 payload={'research_contract':'BACKTEST_COMPARISON_V1','candidate_kind':'RESEARCH_V2_CANDIDATE_V9_VOLATILITY_EXPANSION','status':'RESEARCH_ONLY_NOT_PROMOTED','read_only':True,'evaluation_window':{'start':start,'end':end},'requested_symbols':requested,'results':{k:_run(k,h,requested,successful,start,end) for k,h in schedules.items()},'guards':['production Research V2 unchanged','fixed price-only compression breakout rule','W3/W4/W5 unchanged','no historical orderbook/liquidation fabrication']}
 out=root.parent if root.name=='cache' else root;write_json_cache(out/'research_v2_candidate_v9_volatility_expansion.json',payload);return payload
def parse_args():
 p=argparse.ArgumentParser();p.add_argument('--cache-root',default=str(DEFAULT_CACHE_ROOT));p.add_argument('--symbols');p.add_argument('--schedule',choices=sorted(SCHEDULES));return p.parse_args()
if __name__=='__main__':print(json.dumps(_jsonable(main(parse_args())),indent=2))
