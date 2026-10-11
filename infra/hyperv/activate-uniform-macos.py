"""Activate verified owned Mac guests, preserving the original guest for rollback."""
import argparse,hashlib,json,os,shutil,subprocess,sys
from pathlib import Path
sys.path.insert(0,'/opt/uniform-resources')
sys.path.insert(0,'/tmp')
from agent.config import load
from agent.__main__ import runtime_for
from agent.runtimes.guest_ssh import GuestExec,GuestFs
from agent.runtimes.macos_appliance import MacApplianceRuntime
import importlib.util
s=importlib.util.spec_from_file_location('migration','/tmp/migrate-uniform-macos.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
ROOT=Path('/var/lib/runner-appliances/uniform-activation-20261011');ROOT.mkdir(mode=0o700,parents=True,exist_ok=True);os.chmod(ROOT,0o700)
CONFIG=Path('/etc/runner-agent/agent.json');ORIGINAL=ROOT/'agent.original.json'
MODULES=('runtimes/macos_appliance.py','runtimes/macos_pool.py')
def command(args):subprocess.run(args,check=True,timeout=180)
def save(name,data):
 temp=ROOT/(name+'.writing')
 with temp.open('w') as f:os.chmod(temp,0o600);json.dump(data,f);f.flush();os.fsync(f.fileno())
 os.replace(temp,ROOT/name)
def prepared():
 assert json.loads(Path('/var/lib/runner-appliances/uniform-preparation-20261011/ready.json').read_text())['guests']==2
 for rid in m.IDS:
  installed=json.loads((m.ROOT/rid/'installed.json').read_text())
  assert installed['runner_id']==rid and installed['restart_proved'] is True
  assert (installed['memory_gib'],installed['cores'],installed['storage_gib'],installed['cache_gib'])==(16,8,100,40)
 config,pool=m.build()
 for rid in m.IDS:
  r=pool._read(rid);assert r['initialized'] and r['listener_disabled'] and pool._power(rid)=='stopped'
 new=load('/etc/runner-agent/agent.uniform-pool.json');runtime_for(new)
 return config,pool

def switch():
 if json.loads(CONFIG.read_text()).get('appliance_pool'):
  assert ORIGINAL.exists(), 'Pool configuration lacks its original backup'
  unit=json.loads(subprocess.check_output(['docker','inspect','macos-sequoia']))[0]
  assert unit['State']['Running'] is False, 'Original guest must stay stopped after activation'
  save('switched.json',{'separate_guests':2,'registration_preserved':True,'original_data_retained':True})
  print('Pool configuration already activated; no duplicate switch.',flush=True);return
 prepared()
 original,guest,inner=m.source();m.stopped(inner)
 for i,rid in enumerate(m.IDS):
  captured=json.loads((m.ROOT/rid/'capture.json').read_text());paths=inner.paths(rid)
  home=paths['reg']+'/agent' if captured['expected']['provider']=='github' else captured['legacy_directory'] or paths['reg']
  assert m.registration(inner._fs,home,captured['expected']['provider'])==captured['registration']
 if ORIGINAL.exists():assert ORIGINAL.read_bytes()==CONFIG.read_bytes()
 else:shutil.copy2(CONFIG,ORIGINAL);os.chmod(ORIGINAL,0o600)
 # Stop only the management agent. Both source listeners were already drained;
 # neither registrar is called and the old guest remains recoverable.
 command(['systemctl','stop','runner-agent.service'])
 try:
  command(['python3','/tmp/maintenance-macos-shutdown.py','shutdown'])
  for relative in MODULES:
   live=Path('/opt/runner-agent/agent')/relative;before=ROOT/(relative.replace('/','-')+'.before')
   if not before.exists():shutil.copy2(live,before)
   temporary=live.with_name(live.name+'.uniform-prepared')
   shutil.copy2(Path('/opt/uniform-resources/agent')/relative,temporary);os.replace(temporary,live)
  staged=CONFIG.with_suffix('.uniform-switching')
  shutil.copy2('/etc/runner-agent/agent.uniform-pool.json',staged);os.chmod(staged,0o600);os.replace(staged,CONFIG)
  command(['systemctl','start','runner-agent.service'])
  save('switched.json',{'separate_guests':2,'registration_preserved':True,'original_data_retained':True})
  print('Verified pool activated with both listeners still disabled.',flush=True)
 except Exception:
  if ORIGINAL.exists():shutil.copy2(ORIGINAL,CONFIG)
  for relative in MODULES:
   before=ROOT/(relative.replace('/','-')+'.before')
   if before.exists():shutil.copy2(before,Path('/opt/runner-agent/agent')/relative)
  command(['python3','/tmp/maintenance-macos-shutdown.py','recover'])
  command(['systemctl','start','runner-agent.service'])
  raise

def rollback():
 assert ORIGINAL.exists(), 'No original configuration checkpoint'
 _,pool=m.build()
 for rid in m.IDS:
  r=pool._read(rid)
  if pool._power(rid)=='running':
   inner=pool._context(r)[1];assert inner.status(rid).get('running') is False, 'Refuse rollback of an active target listener'
   inner.stop(rid);pool._write(r,listener_disabled=True);pool._shutdown(r)
  assert pool._power(rid)=='stopped'
 command(['systemctl','stop','runner-agent.service'])
 shutil.copy2(ORIGINAL,CONFIG)
 for relative in MODULES:
  before=ROOT/(relative.replace('/','-')+'.before')
  if before.exists():shutil.copy2(before,Path('/opt/runner-agent/agent')/relative)
 command(['python3','/tmp/maintenance-macos-shutdown.py','recover'])
 command(['systemctl','start','runner-agent.service'])
 save('rolled-back.json',{'source_restored':True,'targets_retained':True})
 print('Original guest and configuration restored; registrations unchanged.',flush=True)

p=argparse.ArgumentParser();p.add_argument('mode',choices=('preflight','switch','rollback','status'));a=p.parse_args()
if a.mode=='switch':switch()
elif a.mode=='rollback':rollback()
elif a.mode=='status':print('pool' if json.loads(CONFIG.read_text()).get('appliance_pool') else 'source')
else:prepared();print('Both targets are verified, disabled and stopped; source untouched.',flush=True)
