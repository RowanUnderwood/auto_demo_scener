import json
import logging
import re
import threading

log = logging.getLogger(__name__)

# ── Validation synchronisation ─────────────────────────────────────────────────
_event = threading.Event()
_result: dict | None = None
_lock = threading.Lock()


def start_validation() -> None:
    global _result
    with _lock:
        _result = None
    _event.clear()


def receive_result(result: dict) -> None:
    global _result
    with _lock:
        _result = result
    _event.set()
    log.debug("Validation result received: %s", result.get("result"))


def wait_for_result(timeout: float) -> dict | None:
    _event.wait(timeout=timeout)
    with _lock:
        return _result


# ── Video-recording synchronisation ─────────────────────────────────────────────
_rec_event = threading.Event()
_rec_result: dict | None = None
_rec_lock = threading.Lock()


def start_recording() -> None:
    global _rec_result
    with _rec_lock:
        _rec_result = None
    _rec_event.clear()


def receive_recording(result: dict) -> None:
    global _rec_result
    with _rec_lock:
        _rec_result = result
    _rec_event.set()
    log.debug("Recording result received: %s", result.get("status"))


def wait_for_recording(timeout: float) -> dict | None:
    _rec_event.wait(timeout=timeout)
    with _rec_lock:
        return _rec_result


# ── HTML manipulation ──────────────────────────────────────────────────────────

PROBE_JS = r"""
(function(){
  var _drawCount=0, _frameCount=0, _lastFpsTs=performance.now();
  var _hasDrawn=false, _errorSent=false, _glCtxs=[];

  function send(msg){ try{ window.parent.postMessage(msg,'*'); }catch(e){} }

  window.onerror=function(msg,src,line,col,err){
    if(_errorSent)return; _errorSent=true;
    send({type:'probe_error',error:String(msg)+' @ '+src+':'+line});
  };
  window.addEventListener('unhandledrejection',function(e){
    if(_errorSent)return; _errorSent=true;
    send({type:'probe_error',error:String(e.reason||'Unhandled rejection')});
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
      _glCtxs.push(c);
    }
    return c;
  };

  function sampleBright(gl){
    var w=gl.drawingBufferWidth,h=gl.drawingBufferHeight;
    if(!w||!h)return 0;
    var bright=0,px=new Uint8Array(4);
    for(var xi=0;xi<4;xi++)for(var yi=0;yi<4;yi++){
      gl.readPixels(Math.floor((xi+0.5)/4*w),Math.floor((yi+0.5)/4*h),1,1,gl.RGBA,gl.UNSIGNED_BYTE,px);
      if(0.299*px[0]+0.587*px[1]+0.114*px[2]>8)bright++;
    }
    return bright;
  }

  function tick(ts){
    _frameCount++;
    requestAnimationFrame(tick);
    var elapsed=ts-_lastFpsTs;
    if(elapsed>=1000){
      var bright=0;
      for(var _i=0;_i<_glCtxs.length;_i++){try{bright=Math.max(bright,sampleBright(_glCtxs[_i]));}catch(e){}}
      send({type:'probe_heartbeat',fps:_frameCount/(elapsed/1000),draws:_drawCount,hasDrawn:_hasDrawn,
            brightPixels:_glCtxs.length>0?bright:null});
      _frameCount=0; _drawCount=0; _lastFpsTs=ts;
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
      if(!canvas){ console.warn('[probe] no canvas found among',_glCtxs.length,'stashed contexts'); send({type:'probe_record_error',error:'no canvas found'}); return; }
      if(!canvas.captureStream){ console.warn('[probe] canvas.captureStream unsupported'); send({type:'probe_record_error',error:'canvas.captureStream unsupported in this browser'}); return; }
      if(!window.MediaRecorder){ console.warn('[probe] MediaRecorder unsupported'); send({type:'probe_record_error',error:'MediaRecorder unsupported in this browser'}); return; }
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
        send({type:'probe_record_error',
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
          .catch(function(e){ console.error('[probe] upload failed',e); send({type:'probe_record_error',error:String(e)}); });
      };
      _mediaRecorder.start();
      console.log('[probe] recorder started, state:',_mediaRecorder.state,'will stop in',(m.seconds||30),'s');
      setTimeout(function(){
        if(_mediaRecorder&&_mediaRecorder.state!=='inactive')_mediaRecorder.stop();
      },(m.seconds||30)*1000);
    }catch(e){ console.error('[probe] recording setup threw',e); send({type:'probe_record_error',error:String(e)}); }
  });

  send({type:'probe_ready'});
})();
"""

_PROBE_SCRIPT_TAG = f"<script>{PROBE_JS}</script>\n"


def inject_probe(html: str, extra_imports: dict | None = None) -> str:
    """Inject probe script (and optional importmap) into the HTML <head>."""

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
    return html[:pos] + _PROBE_SCRIPT_TAG + html[pos:]


def strip_fences(text: str) -> str:
    """Remove ```html ... ``` or ``` ... ``` wrapping, if present."""
    m = re.search(r"```(?:html)?\s*\n([\s\S]*?)\n```", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text.strip()
