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
  return new Promise((resolve, reject) => {
    let socket, retry, finished = false;
    const cleanup = () => {
      finished = true;
      clearTimeout(retry);
      window.removeEventListener('pagehide', cleanup);
      if (socket) { socket.onclose = null; socket.close(); }
    };
    function connect() {
      socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') +
        location.host + '/ws/v2/operations/' + encodeURIComponent(id));
      socket.onmessage = event => {
        try {
          const frame = JSON.parse(event.data);
          if (frame.type !== 'operation') return;
          if (frame.error) { cleanup(); reject(new Error(frame.error)); return; }
          const op = frame.data;
          let result = op.result;
          try { if (typeof result === 'string') result = JSON.parse(result); } catch(e) {}
          const freed = result && Number.isFinite(Number(result.total_bytes)) && Number(result.total_bytes) > 0
            ? ' · freed ' + (Number(result.total_bytes) / 1e9).toFixed(1) + ' GB' : '';
          report(op.verb.replaceAll('_',' ') + ' · ' + op.state +
            (op.error ? ' · ' + op.error : '') + freed, op.state === 'failed', op);
          if (['succeeded','failed','cancelled'].includes(op.state)) { cleanup(); resolve(op); }
        } catch(e) { cleanup(); reject(e); }
      };
      socket.onclose = () => { if (!finished) retry = setTimeout(connect, 5000); };
      socket.onerror = () => socket.close();
    }
    window.addEventListener('pagehide', cleanup);
    connect();
  });
}
