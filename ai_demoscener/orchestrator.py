import base64
import csv
import hashlib
import logging
import random
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from io import StringIO
from pathlib import Path

import archive as archive_mod
import config as cfg_mod
import lm_client
import stats as stats_mod
import validator

log = logging.getLogger(__name__)
BASE = Path(__file__).parent


class Orchestrator:
    def __init__(self, broadcast_fn):
        self._broadcast = broadcast_fn
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._skip = threading.Event()
        self._discard_current = threading.Event()
        self._lm_unavailable = False
        self._last_display_meta: dict | None = None
        self._last_raw_video: Path | None = None
        self._stock_deck: list[dict] = []
        self._replay_deck: list[Path] = []
        self.state = "IDLE"
        self.current_file: Path | None = None
        self.last_state_msg: dict = {"type": "state", "state": "IDLE"}

    # ── Control ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="orchestrator")
        self._thread.start()
        log.info("Orchestrator started")

    def stop(self) -> None:
        self._stop.set()

    def skip(self) -> None:
        self._skip.set()

    def discard_current(self) -> None:
        self._discard_current.set()
        self._skip.set()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _tx(self, state: str, **kw) -> None:
        self.state = state
        msg = {"type": "state", "state": state, **kw}
        self.last_state_msg = msg
        log.info("STATE → %s %s", state, kw or "")
        self._broadcast(msg)

    def _push_token(self, tok: str) -> None:
        self._broadcast({"type": "token", "token": tok})

    def _push_status(self, text: str) -> None:
        self._broadcast({"type": "status", "text": text})

    def _active_provider_cfg(self, cfg: dict) -> tuple[str, dict]:
        provider = cfg.get("llm_provider", "lm_studio")
        if provider not in ("lm_studio", "ninfer"):
            provider = "lm_studio"
        return provider, cfg[provider]

    # ── Main loop ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                cfg = cfg_mod.load()
                if cfg["boot_mode"] == "replay":
                    self._replay_cycle(cfg)
                elif cfg.get("lm_fallback_to_replay", True):
                    self._probe_lm(cfg)
                    if self._lm_unavailable:
                        provider, _ = self._active_provider_cfg(cfg)
                        self._push_status(f"{provider} unreachable — replaying archive")
                        self._replay_cycle(cfg)
                    else:
                        self._generate_cycle(cfg)
                else:
                    self._generate_cycle(cfg)
            except Exception:
                log.exception("Unhandled error in orchestrator")
                time.sleep(10)

    # ── Replay ─────────────────────────────────────────────────────────────────

    def _replay_cycle(self, cfg: dict) -> None:
        if not self._replay_deck:
            files = archive_mod.list_files(cfg)
            if not files:
                log.info("Archive empty; sleeping")
                self._push_status("ARCHIVE EMPTY — waiting for demos")
                time.sleep(15)
                return
            self._replay_deck = files.copy()
            random.shuffle(self._replay_deck)
            log.info("Replay deck refilled: %d files", len(self._replay_deck))
        path = None
        while self._replay_deck:
            candidate = self._replay_deck.pop()
            if candidate.exists():
                path = candidate
                break
        if path is None:
            return  # all candidates were deleted; next cycle refills
        entry = archive_mod.get_meta(path.name, cfg)
        entry_mode   = entry.get("mode", "")
        entry_effect = entry.get("effect", "")
        entry_model  = entry.get("model", "")
        stats_key = entry_effect if entry_mode == "stock" and entry_effect else entry_mode or "unknown"
        meta = {
            "effect":      entry_effect,
            "title":       entry.get("title", ""),
            "description": entry.get("description", ""),
            "model":       entry_model,
        }
        runtime = cfg["display"]["demo_runtime_seconds"]

        # JIT titling: generate title synchronously before displaying
        if cfg["display"].get("jit_titling") and not meta["title"] and not self._lm_unavailable:
            result = self._generate_title_meta(
                cfg, path.read_text(encoding="utf-8"), model=entry_model
            )
            if result:
                meta.update(result)
                archive_mod.update_meta(path.name, result, cfg)

        stats_summary = self._meta_stats(stats_key, entry_model)
        meta["stats"] = stats_summary
        self._tx("DISPLAY", url=f"/archive/{path.name}", runtime=runtime, meta=meta)

        # Background enrichment only if JIT is off and title still missing
        if not cfg["display"].get("jit_titling") and not meta["title"] and not self._lm_unavailable:
            _path = path
            _entry_model = entry_model
            def _enrich():
                try:
                    html = _path.read_text(encoding="utf-8")
                    result = self._generate_title_meta(cfg_mod.load(), html, model=_entry_model)
                    if result:
                        archive_mod.update_meta(_path.name, result, cfg_mod.load())
                        log.info("Enriched title for %s", _path.name)
                except Exception:
                    log.exception("Title enrichment failed for %s", _path.name)
            threading.Thread(target=_enrich, daemon=True).start()
        self._run_timer(runtime)

    def _probe_lm(self, cfg: dict) -> None:
        provider, lm = self._active_provider_cfg(cfg)
        try:
            lm_client.list_models(lm["base_url"])
            if self._lm_unavailable:
                log.info("%s reachable again — resuming generate mode", provider)
                self._push_status(f"{provider} reconnected — resuming generate mode")
            self._lm_unavailable = False
        except lm_client.LMClientError:
            self._lm_unavailable = True

    # ── Generate cycle ─────────────────────────────────────────────────────────

    def _generate_cycle(self, cfg: dict) -> None:
        self._skip.clear()
        self._discard_current.clear()

        # PICK MODE
        self._tx("PICK_MODE")
        mode, prompt_data = self._pick_mode(cfg)
        if mode is None:
            time.sleep(5)
            return

        # Resolve model (surprise me picks at random)
        _, lm_cfg = self._active_provider_cfg(cfg)
        model = lm_cfg["model"]
        if lm_cfg.get("surprise_me"):
            try:
                available = lm_client.list_models(lm_cfg["base_url"], exclude_embedding=True)
                if available:
                    model = random.choice(available)
                    self._broadcast({"type": "model_selected", "model": model})
                    log.info("Surprise Me selected model: %s", model)
            except lm_client.LMClientError:
                pass  # fall back to configured model

        # GENERATE
        self._tx("GENERATE", mode=mode, model=model)
        html = self._do_generate(cfg, mode, prompt_data, model=model)
        if html is None:
            self._tx("DISCARD")
            return

        # Save to temp for audition
        temp_path = self._save_temp(html)

        # VALIDATE + REPAIR loop
        max_repairs = cfg["validation"]["max_repair_attempts"]
        audition_secs = cfg["validation"]["audition_seconds"]
        html, last_error = self._validate_and_repair(
            cfg, temp_path, html, model, max_repairs, audition_secs
        )
        if html is None:
            kind = (last_error or {}).get("kind")
            if kind:
                if mode == "stock" and isinstance(prompt_data, dict):
                    stats_mod.record_failure(
                        prompt_data.get("effect", "unknown"), kind, model=model
                    )
                elif mode in ("creative", "update"):
                    stats_mod.record_failure(mode, kind, model=model)
                self._tx("DISCARD", kind=kind, error=(last_error or {}).get("msg"))
            else:
                self._tx("DISCARD")
            temp_path.unlink(missing_ok=True)
            return

        # DISPLAY
        source_path = prompt_data.get("source_path") if isinstance(prompt_data, dict) else None
        effect_slug = prompt_data.get("effect", mode) if isinstance(prompt_data, dict) else mode

        provider, ninfer_cfg = self._active_provider_cfg(cfg)
        video_check_enabled = provider == "ninfer" and ninfer_cfg.get("video_check_enabled")

        html = self._display_phase(
            cfg, html, temp_path, effect_slug, mode, model, source_path,
            capture_video_seconds=30 if video_check_enabled else None,
        )
        if html is None:
            log.info("Generate-mode demo discarded by user before archiving")
            self._tx("IDLE")
            return
        display_meta_result = self._last_display_meta
        raw_video_path = self._last_raw_video if video_check_enabled else None

        # Optional Ninfer video-improve pass, using the video captured during the display
        # above (may re-display an improved version if it validates successfully)
        if raw_video_path is not None:
            html2 = self._video_improve_from_recording(
                cfg, html, raw_video_path, temp_path, model, effect_slug, mode, source_path
            )
            if html2 is None:
                log.info("Generate-mode demo discarded by user before archiving (video-improve re-display)")
                self._tx("IDLE")
                return
            html = html2
            if self._last_display_meta is not None:
                display_meta_result = self._last_display_meta

        self._tx("ARCHIVE")
        archive_path = archive_mod.save(html, effect_slug, mode, cfg,
                                        source_path=source_path, model=model)
        temp_path.unlink(missing_ok=True)
        if display_meta_result:
            archive_mod.update_meta(archive_path.name, display_meta_result, cfg)

        # Record successful run in stats
        if mode == "stock" and isinstance(prompt_data, dict):
            stats_mod.record_run(prompt_data.get("effect", "unknown"), model=model)
        elif mode in ("creative", "update"):
            stats_mod.record_run(mode, model=model)

        self._tx("IDLE")

    def _validate_and_repair(self, cfg: dict, temp_path: Path, html: str, model: str,
                              max_repairs: int, audition_secs: float) -> tuple[str | None, dict | None]:
        """Runs the audition→repair cycle against temp_path/html.

        Returns (final_html, None) on success, or (None, last_error) if repairs were
        exhausted or a repair call itself failed. Does not record stats, does not broadcast
        DISCARD, and does not delete temp_path on failure — the caller decides what
        exhaustion means in its context (e.g. a first-generation failure discards the whole
        cycle, but a failed video-improve pass just falls back to the original demo).
        """
        last_error: dict = {"kind": None, "msg": None}
        for attempt in range(max_repairs + 1):
            self._tx("VALIDATE", attempt=attempt)
            result = self._audition(temp_path, audition_secs)

            if result.get("result") == "OK":
                return html, None

            last_error = {
                "kind": result.get("result", "CRASHED"),
                "msg": result.get("error", ""),
            }

            if attempt >= max_repairs:
                log.info("Max repairs reached")
                return None, last_error

            error_msg = last_error["msg"] or "unknown error"
            if cfg["display"].get("show_retry_messages", True):
                self._push_status(
                    f"ERROR DETECTED — REQUESTING REPAIR — ATTEMPT {attempt + 1}/{max_repairs}"
                )

            self._tx("REPAIR", attempt=attempt + 1, max=max_repairs)
            html = self._do_repair(cfg, html, error_msg, model=model)
            if html is None:
                return None, {"kind": None, "msg": "repair call failed"}
            temp_path.write_text(html, encoding="utf-8")

        return None, last_error

    def _display_phase(self, cfg: dict, html: str, temp_path: Path, effect_slug: str,
                        mode: str, model: str, source_path: Path | None,
                        capture_video_seconds: float | None = None) -> str | None:
        """Runs one full display cycle for `html`: title/meta generation (JIT or background,
        per the jit_titling setting), the DISPLAY state broadcast, the runtime timer, and the
        discard check. If capture_video_seconds is given, also records the demo concurrently
        with the runtime timer (in a background thread, same pattern as background title
        generation) so a video-improve pass costs no extra display time.

        Returns `html` unchanged on normal completion (ready for the caller to archive), or
        None if the user discarded during this display (caller must skip archiving and go to
        IDLE). Sets self._last_display_meta to the generated title/description dict (or None),
        and self._last_raw_video to the recorded video path (or None), so the caller can use
        them after this returns.
        """
        runtime = cfg["display"]["demo_runtime_seconds"]
        stats_key = effect_slug if mode == "stock" else mode
        stats_summary = self._meta_stats(stats_key, model)
        self._last_display_meta = None
        self._last_raw_video = None

        jit_titling = cfg["display"].get("jit_titling")
        meta_thread = None
        _meta_result: list = [None]

        if jit_titling:
            jit_result = self._generate_title_meta(cfg, html, model=model)
            self._last_display_meta = jit_result
            display_meta = {
                "effect":      effect_slug,
                "model":       model,
                "title":       jit_result.get("title", "") if jit_result else "",
                "description": jit_result.get("description", "") if jit_result else "",
                "stats":       stats_summary,
            }
        else:
            display_meta = {
                "effect":      effect_slug,
                "model":       model,
                "title":       "",
                "description": "",
                "stats":       stats_summary,
            }

        self._tx("DISPLAY", url=f"/temp/{temp_path.name}", runtime=runtime, meta=display_meta)

        if not jit_titling:
            def _fetch_meta():
                _meta_result[0] = self._generate_title_meta(cfg, html, model=model)
            meta_thread = threading.Thread(target=_fetch_meta, daemon=True)
            meta_thread.start()

        video_result: list = [None]
        video_thread = None
        if capture_video_seconds:
            record_secs = min(capture_video_seconds, runtime)
            def _record():
                video_result[0] = self._record_video(seconds=record_secs, fps=2)
            video_thread = threading.Thread(target=_record, daemon=True)
            video_thread.start()

        self._run_timer(runtime)

        if self._discard_current.is_set():
            self._discard_current.clear()
            temp_path.unlink(missing_ok=True)
            return None

        if meta_thread is not None:
            meta_thread.join(timeout=10)
            self._last_display_meta = _meta_result[0]

        if video_thread is not None:
            video_thread.join(timeout=10)
            self._last_raw_video = video_result[0]

        return html

    # ── Ninfer video-improve pass ──────────────────────────────────────────────

    def _video_improve_from_recording(self, cfg: dict, html: str, raw_path: Path, temp_path: Path,
                                       model: str, effect_slug: str, mode: str,
                                       source_path: Path | None) -> str | None:
        """Given a video already recorded during the first display, transcodes it, asks Qwen
        for a visual improvement, validates the result, and re-displays it live on success.

        Returns the html to archive, or None if the user discarded during the improved
        re-display (caller must skip archiving entirely, matching a discard during the first
        display). Any failure along the way (transcode, LLM call, or re-validation) silently
        falls back to returning the original `html` unchanged.
        """
        mp4_path: Path | None = None
        try:
            mp4_path = self._transcode_for_vision(raw_path)
            if mp4_path is None:
                log.warning("ffmpeg transcode failed or ffmpeg missing; skipping video-improve pass")
                return html

            self._tx("VIDEO_IMPROVE", mode=mode, model=model)
            improved = self._do_video_improve(cfg, html, mp4_path, model=model)
            if not improved:
                return html

            max_repairs = cfg["validation"]["max_repair_attempts"]
            audition_secs = cfg["validation"]["audition_seconds"]
            temp_path.write_text(improved, encoding="utf-8")
            final_html, err = self._validate_and_repair(
                cfg, temp_path, improved, model, max_repairs, audition_secs
            )
            if final_html is None:
                log.warning("Video-improved HTML failed validation (%s); archiving original instead", err)
                temp_path.write_text(html, encoding="utf-8")
                return html

            return self._display_phase(cfg, final_html, temp_path, effect_slug, mode, model, source_path)
        except Exception:
            log.exception("Video-improve pass failed; keeping original demo")
            temp_path.write_text(html, encoding="utf-8")
            return html
        finally:
            raw_path.unlink(missing_ok=True)
            if mp4_path is not None:
                mp4_path.unlink(missing_ok=True)

    def _record_video(self, seconds: float, fps: int) -> Path | None:
        record_id = uuid.uuid4().hex[:8]
        validator.start_recording()
        self._broadcast({
            "type": "record_video",
            "seconds": seconds,
            "fps": fps,
            "record_id": record_id,
        })
        result = validator.wait_for_recording(timeout=seconds + 20)
        if result is None:
            log.warning(
                "Video recording timed out after %.0fs waiting for the browser to upload "
                "(no probe_record_done/probe_record_error/upload was received at all — check "
                "whether the record_video WS message and the postMessage relay into the iframe "
                "are actually arriving)",
                seconds + 20,
            )
            return None
        if result.get("status") != "uploaded":
            log.warning("Video recording failed: %s", result.get("error", "unknown error"))
            return None
        path = result.get("path")
        return Path(path) if path else None

    def _transcode_for_vision(self, raw_path: Path) -> Path | None:
        if shutil.which("ffmpeg") is None:
            log.warning("ffmpeg not found on PATH; video-check feature unavailable this cycle")
            return None
        out_path = raw_path.with_suffix(".vision.mp4")
        cmd = [
            "ffmpeg", "-y", "-i", str(raw_path),
            "-vf", "fps=2,scale='min(768,iw)':'min(768,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(out_path),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("ffmpeg transcode failed: %s", e)
            return None
        if r.returncode != 0 or not out_path.exists():
            stderr_tail = r.stderr[-500:].decode(errors="replace") if r.stderr else ""
            log.warning("ffmpeg transcode failed (exit %d): %s", r.returncode, stderr_tail)
            return None
        return out_path

    def _do_video_improve(self, cfg: dict, html: str, mp4_path: Path, model: str = "") -> str | None:
        _, lm = self._active_provider_cfg(cfg)
        prompt = cfg["system_prompts"].get("video_improve", "")
        if not prompt:
            return None
        b64 = base64.b64encode(mp4_path.read_bytes()).decode("ascii")
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": [
                {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{b64}"}},
                {"type": "text", "text": html},
            ]},
        ]
        # Reuses the same streaming path as generate/repair (chat_stream already forwards
        # multimodal content verbatim) so the improved code visibly types into the editor
        # during the VIDEO_IMPROVE state, exactly like GENERATE/REPAIR do.
        max_tokens_override = lm.get("video_max_tokens", lm["max_tokens"])
        improved, _err = self._stream(cfg, messages, model=model, max_tokens_override=max_tokens_override)
        return improved

    def _run_timer(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._stop.is_set() or self._skip.is_set():
                break
            time.sleep(0.25)
        self._skip.clear()

    # ── Mode selection ─────────────────────────────────────────────────────────

    def _pick_mode(self, cfg: dict) -> tuple[str | None, dict | None]:
        weights = cfg["generate_mode_weights"]
        pool = [(m, w) for m, w in weights.items() if w > 0]
        if not pool:
            log.warning("All mode weights are 0")
            return None, None

        total = sum(w for _, w in pool)
        r = random.uniform(0, total)
        cumulative = 0.0
        chosen = pool[-1][0]
        for m, w in pool:
            cumulative += w
            if r <= cumulative:
                chosen = m
                break

        if chosen == "update":
            _, lm = self._active_provider_cfg(cfg)
            max_input = max(0, lm["context_ceiling_tokens"] - lm["max_tokens"])
            src = archive_mod.pick_for_update(cfg, max_input)
            if src is None:
                log.warning(
                    "Update mode: no suitable archive files (max_input=%d tokens, %d files checked)",
                    max_input, len(archive_mod.list_files(cfg))
                )
                chosen = "stock"
            else:
                return "update", {"effect": "update", "source_path": src}

        if chosen == "stock":
            row = self._pick_csv_row()
            return ("stock", row) if row else (None, None)

        # creative
        return "creative", {"effect": "creative"}

    def _pick_csv_row(self) -> dict | None:
        csv_path = BASE / "prompts.csv"
        if not csv_path.exists():
            log.error("prompts.csv not found")
            return None
        if not self._stock_deck:
            try:
                with open(csv_path, "r", encoding="utf-8", newline="") as f:
                    rows = list(csv.DictReader(f))
            except Exception:
                log.exception("Failed to read prompts.csv")
                return None
            if not rows:
                return None
            self._stock_deck = rows.copy()
            random.shuffle(self._stock_deck)
            log.info("Stock deck refilled: %d prompts", len(self._stock_deck))
        row = self._stock_deck.pop()
        return {
            "effect": row.get("Effect", "unknown"),
            "prompt": row.get("Three.js prompt", ""),
        }

    def _csv_text_for_creative(self, cfg: dict) -> str:
        csv_path = BASE / "prompts.csv"
        if not csv_path.exists():
            return ""
        _, lm = self._active_provider_cfg(cfg)
        max_chars = (lm["context_ceiling_tokens"] - lm["max_tokens"]) * 4 - 500
        content = csv_path.read_text(encoding="utf-8")
        if len(content) <= max_chars:
            return content
        reader = csv.reader(StringIO(content))
        rows = list(reader)
        header = rows[:1]
        data = sorted(rows[1:], key=lambda r: len(",".join(r)))
        out = list(header)
        total = len(",".join(header[0])) if header else 0
        for row in data:
            line = ",".join(row)
            if total + len(line) + 1 > max_chars:
                break
            out.append(row)
            total += len(line) + 1
        log.warning("CSV truncated to %d rows for token budget", len(out) - 1)
        import io
        buf = io.StringIO()
        csv.writer(buf).writerows(out)
        return buf.getvalue()

    # ── Generation ─────────────────────────────────────────────────────────────

    def _build_messages(self, cfg: dict, mode: str, prompt_data: dict) -> list[dict]:
        sys_prompts = cfg["system_prompts"]
        if mode == "stock":
            return [
                {"role": "system", "content": sys_prompts["stock"]},
                {"role": "user", "content": prompt_data.get("prompt", "")},
            ]
        if mode == "creative":
            csv_text = self._csv_text_for_creative(cfg)
            return [
                {"role": "system", "content": sys_prompts["creative"]},
                {"role": "user", "content": f"Stock effects for inspiration:\n\n{csv_text}"},
            ]
        if mode == "update":
            src: Path = prompt_data.get("source_path")
            src_html = src.read_text(encoding="utf-8") if src and Path(src).exists() else ""
            return [
                {"role": "system", "content": sys_prompts["update"]},
                {"role": "user", "content": f"Existing demo to update:\n\n{src_html}"},
            ]
        return []

    def _stream(self, cfg: dict, messages: list[dict], model: str = "",
                max_tokens_override: int | None = None) -> tuple[str | None, str | None]:
        """Run the LLM stream; return (html, error_msg)."""
        provider, lm = self._active_provider_cfg(cfg)
        tokens: list[str] = []
        start = time.time()
        extra_params = {"reasoning_effort": lm["thinking_effort"]} if provider == "ninfer" else None
        max_tokens = max_tokens_override if max_tokens_override is not None else lm["max_tokens"]

        try:
            for tok in lm_client.chat_stream(
                messages=messages,
                base_url=lm["base_url"],
                model=model or lm["model"],
                max_tokens=max_tokens,
                temperature=lm["temperature"],
                timeout=lm["request_timeout_seconds"],
                extra_params=extra_params,
            ):
                if self._skip.is_set():
                    log.info("Aborting LLM stream: skip requested")
                    return None, None
                tokens.append(tok)
                self._push_token(tok)
        except lm_client.LengthFinishError:
            log.warning("LLM hit token limit")
            if not tokens:
                return None, "LLM produced no output before token limit"
            # Fall through; repair will fix the truncated output
            html = validator.strip_fences("".join(tokens))
            return html, "output was truncated at token limit"
        except lm_client.LMClientError as e:
            log.error("%s unreachable: %s", provider, e)
            self._lm_unavailable = True
            return None, None

        # Wait for display to drain the typing queue
        total_chars = sum(len(t) for t in tokens)
        display_rate = cfg["display"].get("display_chars_per_sec", 400)
        min_typing = cfg["display"].get("min_typing_seconds", 8)
        elapsed = time.time() - start
        drain_time = total_chars / display_rate
        wait = max(0.0, min_typing - elapsed, drain_time - elapsed)
        if wait > 0:
            time.sleep(wait)

        return validator.strip_fences("".join(tokens)), None

    def _do_generate(self, cfg: dict, mode: str, prompt_data: dict, model: str = "") -> str | None:
        messages = self._build_messages(cfg, mode, prompt_data)
        html, err = self._stream(cfg, messages, model=model)
        if err and html:
            log.warning("Generation had error: %s", err)
        return html

    def _do_repair(self, cfg: dict, html: str, error: str, model: str = "") -> str | None:
        repair_prompt = cfg["system_prompts"]["repair"].format(error=error)
        messages = [
            {"role": "system", "content": repair_prompt},
            {"role": "user", "content": html},
        ]
        repaired, _ = self._stream(cfg, messages, model=model)
        return repaired

    # ── Title / description generation ────────────────────────────────────────

    def _meta_stats(self, effect_key: str, model: str) -> dict:
        raw = stats_mod.load()
        def _row(entry):
            runs  = entry.get("runs", 0)
            fails = entry.get("total", 0)
            return {
                "runs": runs, "fails": fails,
                "deletions": entry.get("deletions", 0),
                "fail_pct": round(fails / runs * 100, 1) if runs else None,
            }
        eff_entry = raw.get(effect_key, {})
        mdl_entry = eff_entry.get("by_model", {}).get(model, {}) if model else {}
        return {"effect": _row(eff_entry), "model": _row(mdl_entry)}

    def _generate_title_meta(self, cfg: dict, html: str, model: str = "") -> dict | None:
        import json as _json
        prompt = cfg["system_prompts"].get("title", "")
        if not prompt:
            return None
        provider, lm = self._active_provider_cfg(cfg)
        use_model = model or lm["model"]
        if provider == "ninfer":
            # Title/description generation is cheap and low-stakes — always use minimal
            # thinking effort here regardless of the user's configured setting, mirroring
            # the /no_think convention used on the lm_studio branch below.
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": html[:6000]},
            ]
            extra_params = {"reasoning_effort": "none"}
        else:
            messages = [
                {"role": "system", "content": prompt},
                # /no_think disables reasoning mode on Qwen3 thinking models; harmless on others
                {"role": "user", "content": "/no_think\n" + html[:6000]},
            ]
            extra_params = None
        text = None
        try:
            text = lm_client.chat(
                messages=messages,
                base_url=lm["base_url"],
                model=use_model,
                max_tokens=None,
                temperature=0.7,
                timeout=120,
                extra_params=extra_params,
            )
            if not text or not text.strip():
                log.warning("Title generation: empty response (model=%s)", use_model)
                return None
            start = text.find('{')
            end   = text.rfind('}')
            if start == -1 or end == -1:
                log.warning(
                    "Title generation: no JSON object in response (model=%s): %r",
                    use_model, text[:300],
                )
                return None
            data = _json.loads(text[start:end + 1])
            return {
                "title": str(data.get("title", "")),
                "description": str(data.get("description", "")),
            }
        except Exception as e:
            log.warning(
                "Title generation failed (model=%s): %s — response: %r",
                use_model, e, (text or "")[:300],
            )
            return None

    # ── Temp file & audition ───────────────────────────────────────────────────

    def _save_temp(self, html: str) -> Path:
        temp_dir = BASE / "temp"
        temp_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")
        h = hashlib.md5(html.encode()).hexdigest()[:6]
        path = temp_dir / f"demo_{ts}_{h}.html"
        path.write_text(html, encoding="utf-8")
        return path

    def _audition(self, path: Path, seconds: float) -> dict:
        validator.start_validation()
        self._broadcast({
            "type": "audition",
            "url": f"/temp/{path.name}",
            "seconds": seconds,
        })
        result = validator.wait_for_result(timeout=seconds + 15)
        if result is None:
            log.warning("Audition timed out — treating as OK")
            return {"result": "OK"}
        return result
