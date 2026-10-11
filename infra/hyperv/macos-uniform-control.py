"""Mac-only cutover gates over the existing controller and native DB events."""
import argparse,json,os,time
from pathlib import Path
import providers
from dbwatch import DatabaseChanges
from control import agent_runtime
from control.forges import LiveForges
from control.secrets import SecretStore
from control.service import RunnerService
from store import schema
IDS=('c7343225-7446-4b87-87a6-cb516522e1dc','fc5bc5c1-65da-4295-9109-58183d9c4d1f')
TEMPLATES=('actions-runner-v2.338.0-macos-r20261008','forgejo-runner-v13.1.0-macos-r20260921')
BASE=Path('/data/macos-uniform-cutover-20261011.json')
FIELDS=('runtime_template','cpu_limit','memory_limit','memory_swap_limit','disk_limit','cache_policy','cpu_override','memory_override','adopt_unit')
GIB=1024**3

def identity(s): return (s['runner_id'],s['host_id'],str(s['registration_id']),s.get('registration_uuid'))
def main():
 p=argparse.ArgumentParser();p.add_argument('mode',choices=('prepare','drain','resume','profile','restore-profile','verify'));p.add_argument('--phase',choices=('capture','activate','rollback'),default='capture');a=p.parse_args()
 service=RunnerService(schema.DB_PATH,runtimes=agent_runtime.TABLE,env=SecretStore(schema.DB_PATH).overlay(os.environ))
 rows=lambda:[service.specs.get(r) for r in IDS]
 if a.mode=='prepare':
  specs=rows();assert all(s['host_id']=='macos-appliance-1' for s in specs)
  assert [str(s['registration_id']) for s in specs]==['2217','4']
  proof={'identities':[identity(s) for s in specs],'fields':{s['runner_id']:{k:s.get(k) for k in FIELDS} for s in specs}}
  if BASE.exists():assert json.loads(BASE.read_text())['identities']==json.loads(json.dumps(proof['identities']))
  else:
   fd=os.open(BASE,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
   with os.fdopen(fd,'w') as f:json.dump(proof,f)
  print('Mac identities and original profiles checkpointed; nothing stopped.',flush=True);return
 saved=json.loads(BASE.read_text());assert [identity(s) for s in rows()]==[tuple(i) for i in saved['identities']]
 watch=DatabaseChanges(schema.DB_PATH);deadline=time.monotonic()+432000
 def wait():
  left=deadline-time.monotonic()
  if left<=0:raise TimeoutError('Mac-only operation remains blocked; no forced stop')
  watch.wait(left)
 def observed(specs):
  f=LiveForges(service.env);records={p.key:f.records(p) for p in providers.ALL}
  return [providers.by_key(s['provider']).job_state(s,records[s['provider']]) for s in specs]
 def quiescent(specs):return all(s['actual_state']=='drained' and not s.get('current_operation') for s in specs)
 try:
  if a.mode=='drain':
   while True:
    specs=rows()
    if quiescent(specs):break
    states=observed(specs) if any(s['desired_state']=='running' and s['actual_state']=='idle' and not s.get('current_operation') for s in specs) else []
    for s,state in zip(specs,states):
     if s['desired_state']=='running' and s['actual_state']=='idle' and not s.get('current_operation') and state in (providers.IDLE,providers.OFFLINE):
      service.act(s['runner_id'],'drain',requested_by='FiLL-authorized-Mac-uniformity',idempotency_key='mac-uniform-'+a.phase+'-drain-'+s['runner_id']+'-v'+str(s['spec_version']))
    wait()
   assert all(x in (providers.IDLE,providers.OFFLINE) for x in observed(rows()))
   print('Both Mac listeners are confirmed drained; other runners were untouched.',flush=True)
  elif a.mode=='resume':
   while True:
    pending=[s for s in rows() if s['desired_state']=='drained']
    if not pending:break
    for s in pending:
     if s['actual_state']=='drained' and not s.get('current_operation') and service.inventory.health(s['host_id'])=='healthy':
      service.act(s['runner_id'],'cancel_drain',requested_by='FiLL-authorized-Mac-uniformity',idempotency_key='mac-uniform-'+a.phase+'-resume-'+s['runner_id']+'-v'+str(s['spec_version']))
    if any(s['desired_state']=='drained' for s in rows()):wait()
   print('Both Mac runners requested back into service with unchanged registrations.',flush=True)
  elif a.mode in ('profile','restore-profile'):
   specs=rows();assert quiescent(specs), 'Profile changes require both Mac listeners drained'
   for i,s in enumerate(specs):
    if a.mode=='profile':
     changes=dict(runtime_template=TEMPLATES[i],cpu_limit=8,memory_limit=16*GIB,memory_swap_limit=16*GIB,disk_limit=100*GIB,cache_policy=dict(s.get('cache_policy') or {},enabled=True,max_bytes=40*GIB),cpu_override=None,memory_override=None,adopt_unit=None)
    else:changes=saved['fields'][s['runner_id']]
    service.specs.update(s['runner_id'],s['spec_version'],**changes)
   print('Two Mac profiles updated; identities and labels retained.',flush=True)
  else:
   while True:
    specs=rows()
    if all(s['desired_state']=='running' and s['actual_state'] in ('idle','busy') and not s.get('current_operation') for s in specs) and all(x in (providers.IDLE,providers.BUSY) for x in observed(specs)):
     proven=True
     for s in specs:
      t=s.get('telemetry') or {};enforcement=t.get('resource_enforcement') or {}
      proven=proven and t.get('cpu_cores')==8 and t.get('mem_limit_bytes')==16*GIB and t.get('cache_cap_bytes')==40*GIB and t.get('storage_total_bytes')==100*GIB and enforcement.get('appliance_per_runner') is True
     if proven:break
    wait()
   assert [identity(s) for s in rows()]==[tuple(i) for i in saved['identities']]
   print(json.dumps({'verified':2,'cpu_cores':8,'memory_gib':16,'storage_gib':100,'cache_gib':40,'separate_guests':True,'identities_preserved':True}),flush=True)
 finally:watch.close()
if __name__=='__main__':main()