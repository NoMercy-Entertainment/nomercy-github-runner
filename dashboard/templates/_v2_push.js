function subscribeFleet(onSnapshot, onFallback) {
  let socket, retry;
  function connect() {
    socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/v2/fleet');
    socket.onmessage = event => {
      try { const frame=JSON.parse(event.data);if(frame.type==='snapshot')onSnapshot(frame.data); }
      catch(e) { console.warn('Invalid fleet update',e); }
    };
    socket.onclose = () => { onFallback();retry=setTimeout(connect,5000); };
    socket.onerror = () => socket.close();
  }
  connect();
  window.addEventListener('pagehide',()=>{clearTimeout(retry);socket.onclose=null;socket.close();});
}

async function watchOperation(id, report) {
  for(let attempt=0;attempt<360;attempt++) {
    const response=await fetch('/api/v2/operations/'+encodeURIComponent(id),{cache:'no-store'});
    const data=await response.json();if(!response.ok)throw new Error(data.error||('HTTP '+response.status));
    const op=data.operation;
    let result=op.result;try{if(typeof result==='string')result=JSON.parse(result);}catch(e){}
    const freed = result && Number.isFinite(Number(result.total_bytes)) && Number(result.total_bytes) > 0
      ? ' · freed ' + (Number(result.total_bytes) / 1e9).toFixed(1) + ' GB' : '';
    report(op.verb.replaceAll('_',' ') + ' · ' + op.state +
      (op.error ? ' · ' + op.error : '') + freed, op.state === 'failed', op);
    if(['succeeded','failed','cancelled'].includes(op.state))return op;
    await new Promise(resolve=>setTimeout(resolve,3000));
  }
  report('Still in progress; view its runner for details.',false,{state:'running'});
}
