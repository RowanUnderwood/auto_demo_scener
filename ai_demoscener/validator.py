import json
import logging
import re
import threading

log = logging.getLogger(__name__)

# ── Validation synchronisation ─────────────────────────────────────────────────
# Each audition / recording carries an ID. Only a reply carrying the pending ID completes
# the wait, so a late reply from a previous audition, or a duplicate from a second display
# tab, can't satisfy the wrong request (the first matching reply wins).
_event = threading.Event()
_result: dict | None = None
_pending_id: str | None = None
_lock = threading.Lock()


def start_validation(audition_id: str) -> None:
    global _result, _pending_id
    with _lock:
        _result = None
        _pending_id = audition_id
    _event.clear()


def receive_result(result: dict) -> None:
    global _result, _pending_id
    with _lock:
        if _pending_id is None or result.get("audition_id") != _pending_id:
            log.debug("Ignoring stale/unexpected audit_result %s (pending %s)",
                      result.get("audition_id"), _pending_id)
            return
        _result = result
        _pending_id = None
    _event.set()
    log.debug("Validation result received: %s", result.get("result"))


def wait_for_result(timeout: float) -> dict | None:
    global _pending_id
    _event.wait(timeout=timeout)
    with _lock:
        if _result is None:
            _pending_id = None  # timed out — a reply arriving now is stale
        return _result


# ── Video-recording synchronisation ─────────────────────────────────────────────
_rec_event = threading.Event()
_rec_result: dict | None = None
_rec_pending_id: str | None = None
_rec_lock = threading.Lock()


def start_recording(record_id: str) -> None:
    global _rec_result, _rec_pending_id
    with _rec_lock:
        _rec_result = None
        _rec_pending_id = record_id
    _rec_event.clear()


def is_pending_recording(record_id: str) -> bool:
    with _rec_lock:
        return _rec_pending_id is not None and record_id == _rec_pending_id


def receive_recording(record_id: str, result: dict) -> None:
    global _rec_result, _rec_pending_id
    with _rec_lock:
        if _rec_pending_id is None or record_id != _rec_pending_id:
            log.debug("Ignoring stale/unexpected recording result %s (pending %s)",
                      record_id, _rec_pending_id)
            return
        _rec_result = result
        _rec_pending_id = None
    _rec_event.set()
    log.debug("Recording result received: %s", result.get("status"))


def wait_for_recording(timeout: float) -> dict | None:
    global _rec_pending_id
    _rec_event.wait(timeout=timeout)
    with _rec_lock:
        if _rec_result is None:
            _rec_pending_id = None
        return _rec_result


# ── Diagnostics ─────────────────────────────────────────────────────────────────

def format_diagnostics(diag: dict) -> str:
    """Render the display page's structured audition evidence as compact text for the
    repair prompt (bounded, since it shares the input budget with the HTML)."""
    lines = []
    for e in (diag.get("errors") or [])[:3]:
        lines.append(f"- error [{e.get('category', 'js')}]: {str(e.get('error', ''))[:1200]}")
    for c in (diag.get("console") or [])[:3]:
        lines.append(f"- console.{c.get('level', 'log')}: {str(c.get('text', ''))[:200]}")
    metrics = []
    if diag.get("render_fps") is not None:
        metrics.append(f"frames with draw calls: {diag['render_fps']}/s")
    if diag.get("heartbeat_fps") is not None:
        metrics.append(f"browser frame rate: {diag['heartbeat_fps']}/s")
    if diag.get("visible_cells") is not None and diag.get("grid_cells"):
        metrics.append(f"lit sample cells: {diag['visible_cells']}/{diag['grid_cells']}")
    if diag.get("frozen_seconds"):
        metrics.append(f"unchanged output for {diag['frozen_seconds']}s")
    if diag.get("context_lost"):
        metrics.append("WebGL context was lost")
    if metrics:
        lines.append("- " + "; ".join(metrics))
    return "\n".join(lines)


# ── HTML manipulation ──────────────────────────────────────────────────────────

PROBE_JS = r"""
(function(){
  /* Audition ID passed in by the orchestrator as ?aid=…; stamped on every message so the
     display page can ignore messages from a previous iframe load. Null outside auditions. */
  var _aid=null;
  try{ _aid=new URLSearchParams(location.search).get('aid'); }catch(e){}

  var _drawCount=0, _drawsAtLastTick=0, _frameCount=0, _renderFrames=0, _lastFpsTs=performance.now();
  var _hasDrawn=false, _glCtxs=[], _lastSig=null, _errCount=0, _conCount=0;
  var MAX_MSGS=10, MAX_LEN=2000;

  /* Number of lines inject_probe() added above the demo's own code; subtracted from line
     numbers in error locations so they match the source the model wrote. Also drops the
     ?aid= query from those URLs. */
  var LINE_OFFSET=__PROBE_LINE_OFFSET__;
  function fixLines(text){
    return String(text).replace(/(https?:\/\/[^\s()?#]+\.html)(\?[^\s:()#]*)?:(\d+)(:\d+)?/g,
      function(m,url,q,line,col){ return url+':'+Math.max(1,(+line)-LINE_OFFSET)+(col||''); });
  }
  function clip(s){ s=fixLines(s); return s.length>MAX_LEN ? s.slice(0,MAX_LEN)+'…' : s; }
  function send(msg){ msg.aid=_aid; try{ window.parent.postMessage(msg,'*'); }catch(e){} }
  /* Every error is reported (bounded), not just the first — the display page collects
     follow-on errors briefly before concluding the audition. */
  function sendError(category,text){
    if(_errCount>=MAX_MSGS)return; _errCount++;
    send({type:'probe_error',category:category,error:clip(text)});
  }

  window.onerror=function(msg,src,line,col,err){
    var text=String(msg)+' @ '+src+':'+line+(col?':'+col:'');
    if(err&&err.stack)text+='\n'+err.stack;
    sendError('js',text);
  };
  window.addEventListener('unhandledrejection',function(e){
    var r=e.reason;
    sendError('promise',(r&&r.stack)?r.stack:String(r||'Unhandled rejection'));
  });

  /* three.js reports shader compile/link failures via console.error rather than throwing,
     so wrap console.error/warn: shader errors count as crashes, the rest is evidence. */
  ['error','warn'].forEach(function(level){
    var orig=console[level];
    console[level]=function(){
      try{
        var text=Array.prototype.map.call(arguments,function(a){
          return (a instanceof Error) ? (a.stack||String(a)) : String(a);
        }).join(' ');
        if(text.indexOf('[probe]')!==0){
          if(level==='error'&&/THREE\.WebGLProgram|Shader Error|Program Info Log/i.test(text)){
            sendError('shader',text);
          }else if(_conCount<MAX_MSGS){
            _conCount++;
            send({type:'probe_console',level:level,text:clip(text)});
          }
        }
      }catch(e){}
      return orig.apply(console,arguments);
    };
  });

  function patchCtx(ctx){
    if(!ctx||ctx.__probed)return; ctx.__probed=true;
    ['drawArrays','drawElements'].forEach(function(fn){
      var orig=ctx[fn].bind(ctx);
      ctx[fn]=function(){ _drawCount++; _hasDrawn=true; return orig.apply(ctx,arguments); };
    });
  }
  var _origGetCtx=HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext=function(t,o){
    if(t==='webgl'||t==='webgl2'||t==='experimental-webgl')
      o=Object.assign({},o,{preserveDrawingBuffer:true});
    var c=_origGetCtx.call(this,t,o);
    if((t==='webgl'||t==='webgl2'||t==='experimental-webgl')&&c){
      patchCtx(c);
      if(_glCtxs.indexOf(c)<0){
        _glCtxs.push(c);
        this.addEventListener('webglcontextlost',function(){ sendError('context_lost','WebGL context lost'); });
      }
    }
    return c;
  };

  /* 8x8 grid: count of visibly lit cells, plus a coarse luma signature for detecting
     frozen output between heartbeats. */
  var GRID=8;
  function sampleGrid(gl){
    var w=gl.drawingBufferWidth,h=gl.drawingBufferHeight;
    if(!w||!h)return {bright:0,sig:''};
    var bright=0,sig='',px=new Uint8Array(4);
    for(var xi=0;xi<GRID;xi++)for(var yi=0;yi<GRID;yi++){
      gl.readPixels(Math.floor((xi+0.5)/GRID*w),Math.floor((yi+0.5)/GRID*h),1,1,gl.RGBA,gl.UNSIGNED_BYTE,px);
      var luma=0.299*px[0]+0.587*px[1]+0.114*px[2];
      if(luma>8)bright++;
      sig+=String.fromCharCode(65+(luma>>4));
    }
    return {bright:bright,sig:sig};
  }

  function tick(ts){
    _frameCount++;
    /* A tick counts as a rendered frame only if the demo issued draw calls since the last
       tick — distinct from the heartbeat (rAF) rate, which runs regardless. */
    if(_drawCount!==_drawsAtLastTick){ _renderFrames++; _drawsAtLastTick=_drawCount; }
    requestAnimationFrame(tick);
    var elapsed=ts-_lastFpsTs;
    if(elapsed>=1000){
      var bright=0,sig='';
      for(var _i=0;_i<_glCtxs.length;_i++){
        try{ var g=sampleGrid(_glCtxs[_i]); bright=Math.max(bright,g.bright); sig+=g.sig; }catch(e){}
      }
      var hasGl=_glCtxs.length>0;
      send({type:'probe_heartbeat',
            fps:_frameCount/(elapsed/1000),
            renderFps:_renderFrames/(elapsed/1000),
            draws:_drawCount,hasDrawn:_hasDrawn,
            brightPixels:hasGl?bright:null,
            gridCells:GRID*GRID*Math.max(1,_glCtxs.length),
            changed:(hasGl&&_lastSig!==null)?sig!==_lastSig:null});
      if(hasGl)_lastSig=sig;
      _frameCount=0; _renderFrames=0; _drawCount=0; _drawsAtLastTick=0; _lastFpsTs=ts;
    }
  }
  requestAnimationFrame(tick);

  /* Silence sandbox storage errors */
  var _fakeStorage={getItem:function(){return null;},setItem:function(){},removeItem:function(){},clear:function(){},length:0,key:function(){return null;}};
  try{ window.localStorage; }catch(e){ Object.defineProperty(window,'localStorage',{get:function(){return _fakeStorage;}}); }
  try{ window.sessionStorage; }catch(e){ Object.defineProperty(window,'sessionStorage',{get:function(){return _fakeStorage;}}); }

  /* Relay keydown events to parent so 'd' works even when iframe has focus */
  document.addEventListener('keydown',function(e){
    send({type:'probe_keydown',key:e.key});
  });

  /* Video-improve recording: dormant unless the parent sends a start_recording command,
     which only the orchestrator does, and only when Ninfer + video-check are enabled. */
  var _mediaRecorder=null, _recordedChunks=[];
  window.addEventListener('message',function(ev){
    var m=ev.data;
    if(!m||m.type!=='start_recording')return;
    console.log('[probe] received start_recording',m,'glCtxs count:',_glCtxs.length);
    try{
      var canvas=null;
      for(var i=_glCtxs.length-1;i>=0;i--){ if(_glCtxs[i].canvas){ canvas=_glCtxs[i].canvas; break; } }
      if(!canvas){ console.warn('[probe] no canvas found among',_glCtxs.length,'stashed contexts'); send({type:'probe_record_error',record_id:m.record_id,error:'no canvas found'}); return; }
      if(!canvas.captureStream){ console.warn('[probe] canvas.captureStream unsupported'); send({type:'probe_record_error',record_id:m.record_id,error:'canvas.captureStream unsupported in this browser'}); return; }
      if(!window.MediaRecorder){ console.warn('[probe] MediaRecorder unsupported'); send({type:'probe_record_error',record_id:m.record_id,error:'MediaRecorder unsupported in this browser'}); return; }
      var stream=canvas.captureStream(m.fps||2);
      console.log('[probe] captureStream ok, tracks:',stream.getTracks().length);
      /* Try the widest reasonable set of containers/codecs — whatever we end up with,
         ffmpeg on the server side re-encodes it to H.264 MP4 regardless, so any of these
         working is fine. Some Linux browsers (WebKitGTK in particular) lack VP8/VP9 support
         without extra GStreamer plugins but do support H.264 in either container. */
      var candidates=['video/webm;codecs=vp9','video/webm;codecs=vp8','video/webm;codecs=h264',
                       'video/webm','video/mp4;codecs=h264','video/mp4'];
      var mimeType='';
      for(var ci=0;ci<candidates.length;ci++){
        if(MediaRecorder.isTypeSupported(candidates[ci])){ mimeType=candidates[ci]; break; }
      }
      if(!mimeType){
        console.warn('[probe] no supported mimeType among',candidates);
        send({type:'probe_record_error',record_id:m.record_id,
              error:'no supported MediaRecorder mimeType (tried: '+candidates.join(', ')+')'});
        return;
      }
      console.log('[probe] using mimeType:',mimeType);
      _recordedChunks=[];
      _mediaRecorder=new MediaRecorder(stream,{mimeType:mimeType});
      _mediaRecorder.ondataavailable=function(e){ if(e.data&&e.data.size)_recordedChunks.push(e.data); console.log('[probe] chunk received, size:',e.data&&e.data.size); };
      _mediaRecorder.onstop=function(){
        console.log('[probe] recorder stopped, chunks:',_recordedChunks.length,'uploading…');
        var blob=new Blob(_recordedChunks,{type:mimeType||'video/webm'});
        fetch('/api/upload_recording?record_id='+encodeURIComponent(m.record_id||''),{method:'POST',body:blob})
          .then(function(){ console.log('[probe] upload complete'); send({type:'probe_record_done',record_id:m.record_id}); })
          .catch(function(e){ console.error('[probe] upload failed',e); send({type:'probe_record_error',record_id:m.record_id,error:String(e)}); });
      };
      _mediaRecorder.start();
      console.log('[probe] recorder started, state:',_mediaRecorder.state,'will stop in',(m.seconds||30),'s');
      setTimeout(function(){
        if(_mediaRecorder&&_mediaRecorder.state!=='inactive')_mediaRecorder.stop();
      },(m.seconds||30)*1000);
    }catch(e){ console.error('[probe] recording setup threw',e); send({type:'probe_record_error',record_id:m.record_id,error:String(e)}); }
  });

  send({type:'probe_ready'});
})();
"""

_PROBE_SCRIPT_TAG = f"<script>{PROBE_JS}</script>\n"
_LINE_OFFSET_PLACEHOLDER = "__PROBE_LINE_OFFSET__"


def inject_probe(html: str, extra_imports: dict | None = None) -> str:
    """Inject probe script (and optional importmap) into the HTML <head>."""
    original_lines = html.count("\n")

    # --- importmap handling ---
    if extra_imports:
        im_pat = re.compile(
            r'(<script[^>]+type=["\']importmap["\'][^>]*>)([\s\S]*?)(</script>)',
            re.IGNORECASE,
        )
        im_match = im_pat.search(html)
        if im_match:
            try:
                existing = json.loads(im_match.group(2).strip())
            except Exception:
                existing = {}
            merged = {**extra_imports, **existing.get("imports", {})}
            new_body = json.dumps({"imports": merged})
            html = html[: im_match.start(2)] + new_body + html[im_match.end(2) :]
        else:
            im_tag = f'<script type="importmap">{json.dumps({"imports": extra_imports})}</script>\n'
            head_m = re.search(r"(<head[^>]*>)", html, re.IGNORECASE)
            if head_m:
                html = html[: head_m.end()] + im_tag + html[head_m.end() :]
            else:
                html = im_tag + html

    # --- probe script ---
    head_m = re.search(r"(<head[^>]*>)", html, re.IGNORECASE)
    if head_m:
        pos = head_m.end()
    else:
        pos = 0
    out = html[:pos] + _PROBE_SCRIPT_TAG + html[pos:]
    # Everything injected sits in <head>, above the demo's scripts, so this one offset maps
    # served line numbers back to the original source.
    offset = out.count("\n") - original_lines
    return out.replace(_LINE_OFFSET_PLACEHOLDER, str(offset), 1)


def strip_fences(text: str) -> str:
    """Remove ```html ... ``` or ``` ... ``` wrapping, if present."""
    m = re.search(r"```(?:html)?\s*\n([\s\S]*?)\n```", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text.strip()
