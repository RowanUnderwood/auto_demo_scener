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
